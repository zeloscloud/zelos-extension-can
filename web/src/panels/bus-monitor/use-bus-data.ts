import {
  type AppBridgePanelInfo,
  type AppBridgePanelSignal,
  fitRowBudget,
  type LatestSignalValue,
  type PanelSubscribeParams,
} from "@zeloscloud/app-extension-sdk";
import { usePanelData, useTimeState, useWorkspace } from "@zeloscloud/app-extension-sdk/react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  buildCanEventRows,
  type CanRow,
  canEventQuerySignals,
  canFrameQuerySignals,
  canPathsKey,
  reduceCanFrameRows,
} from "./data";
import { advanceLatch, markDeepReadDone, panelLatch } from "./latch";
import { toTimeMode } from "./time";

/** 5 Hz. The panel shows one row per id however fast the bus runs, so a display-rate poll buys nothing. */
const CAN_POLL_MS = 200;

/** Rows the frame window fetches per poll. */
const CAN_MAX_ROWS = 10_000;

/** The first window per panel and session reads deep, so an id that recurs slowly is latched at all. */
const CAN_FIRST_MAX_ROWS = 100_000;

interface BusMonitorData {
  rows: CanRow[];
  /** Rows the frame window held before the cap cut it, and the cap, fitted to the host's cell limit. */
  totalRows: number;
  cap: number;
  isLoading: boolean;
  error: string | null;
}

/** A value held to at most one change per `ms`; the final value always lands. */
function useThrottledValue<T>(value: T, ms: number): T {
  const [held, setHeld] = useState(value);
  const lastAt = useRef(0);
  useEffect(() => {
    const wait = lastAt.current + ms - Date.now();
    const land = () => {
      lastAt.current = Date.now();
      setHeld(value);
    };
    if (wait <= 0) {
      land();
      return;
    }
    const timer = setTimeout(land, wait);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return held;
}

/** Each bound path once: one table bound from two producers is two signals with one path. */
function uniquePaths(signals: readonly AppBridgePanelSignal[]): string[] {
  return [...new Set(signals.map((signal) => signal.path))];
}

/** A decimal or exponent literal. An integer's text never goes through a JS number, so a 64-bit id keeps its digits. */
const FLOAT_TEXT = /^-?(?:\d+\.\d*|\.\d+|\d+(?:\.\d*)?[eE][-+]?\d+)$/;

/** A float's shortest text, at most 8 fraction digits; an exponent form stays, so a tiny value never reads 0. */
function trimNumber(text: string): string {
  if (!FLOAT_TEXT.test(text)) return text;
  const n = Number(text);
  if (!Number.isFinite(n)) return text;
  const shortest = n.toString();
  if (shortest.includes("e")) return shortest;
  const fraction = shortest.split(".")[1];
  return fraction && fraction.length > 8 ? n.toFixed(8).replace(/\.?0+$/, "") : shortest;
}

/** The label a value table gives a raw value, keyed by the raw value as text; null on a miss or no table. */
function valueTableLabel(
  value: string,
  table: Record<string, string> | null | undefined,
): string | null {
  if (!table || !Object.hasOwn(table, value)) return null;
  const label = table[value];
  return typeof label === "string" ? label : null;
}

/** A latest value as text: `LABEL (n)` on a value-table hit, so the code stays beside its name, then the unit. */
export function formatLatestValue(
  value: string,
  signal: Pick<AppBridgePanelSignal, "unit" | "dictionary" | "valueTable"> | undefined,
) {
  if (value === "") return "-";
  const rendered = signal?.dictionary ? value : trimNumber(value);
  const label = valueTableLabel(value, signal?.valueTable);
  const shown = label === null ? rendered : signal?.dictionary ? label : `${label} (${rendered})`;
  const unit = signal?.unit?.trim();
  return unit ? `${shown} ${unit}` : shown;
}

/** Frames need a window, since one table carries every id; every other table reads its latest row instead. */
export function useBusMonitorData(panel: AppBridgePanelInfo, stateKey: string): BusMonitorData {
  const { signals } = panel;
  const frameSignals = useMemo(() => canFrameQuerySignals(signals), [signals]);
  const eventSignals = useMemo(() => canEventQuerySignals(signals), [signals]);

  const workspace = useWorkspace();
  const time = useTimeState();
  const live = workspace?.modeKind === "LIVE" && time?.playback === "LIVE";
  // Every position a cursor sweep passes is a different window, so the sweep is held to the poll rate.
  const cursorS = useThrottledValue(time?.cursorS ?? null, CAN_POLL_MS);
  // Live reads the tail and never retreats, so it has no end to track; paused and trace end at the cursor.
  const windowEndS = live
    ? null
    : (cursorS ?? time?.viewRange?.endS ?? time?.dataRange?.endS ?? null);

  const [deepRead, setDeepRead] = useState(() => panelLatch(stateKey).deepReadDone);
  const wantedRows = deepRead ? CAN_MAX_ROWS : CAN_FIRST_MAX_ROWS;
  const framePaths = useMemo(() => uniquePaths(frameSignals), [frameSignals]);
  // The host splits its cell budget over every bound signal, not every path.
  const cap = fitRowBudget(frameSignals.length, wantedRows);
  // An empty list would mean "every bound signal" to the host, so no list means no subscription.
  const frameParams = useMemo<PanelSubscribeParams | null>(
    () =>
      framePaths.length === 0
        ? null
        : {
            id: "frames",
            shape: "rows",
            signals: framePaths,
            maxRows: cap,
            sortOrder: "desc",
            endAtCursor: true,
            minPollMs: CAN_POLL_MS,
          },
    [framePaths, cap],
  );
  const eventParams = useMemo<PanelSubscribeParams | null>(
    () =>
      eventSignals.length === 0
        ? null
        : {
            id: "latest",
            shape: "latest",
            signals: uniquePaths(eventSignals),
            minPollMs: CAN_POLL_MS,
          },
    [eventSignals],
  );
  const frames = usePanelData(frameParams);
  const events = usePanelData(eventParams);
  const framePayload = frames?.dataset ?? null;
  const eventPayload = events?.latest ?? null;

  // The deep read has landed and is latched; every later poll only has to carry what has changed since.
  if (framePayload && !deepRead) setDeepRead(true);
  useEffect(() => {
    if (framePayload) markDeepReadDone(stateKey);
  }, [framePayload, stateKey]);

  const bySignalPath = useMemo(
    () => new Map(signals.map((signal) => [signal.path, signal])),
    [signals],
  );
  const fieldOrder = useMemo(() => {
    const order = new Map<string, number>();
    for (const signal of eventSignals)
      if (!order.has(signal.path)) order.set(signal.path, order.size);
    return order;
  }, [eventSignals]);

  // Another workspace, mode or recording set makes the latched rows another recording's; a signal joining keeps them.
  const scopeKey = [
    workspace?.id ?? "",
    workspace?.modeKind ?? "",
    ...(workspace?.traces ?? []),
  ].join("\0");
  const pathsKey = useMemo(() => canPathsKey(signals.map((signal) => signal.path)), [signals]);
  const frameTimeMode = toTimeMode(frames?.timeMode);
  const eventTimeMode = toTimeMode(events?.timeMode ?? time?.mode);

  const rows = useMemo(
    () =>
      advanceLatch(stateKey, {
        scopeKey,
        pathsKey,
        windowEndS,
        live,
        frames: {
          payload: framePayload,
          build: () => reduceCanFrameRows(framePayload, "desc", frameSignals, frameTimeMode),
        },
        events: {
          payload: eventPayload,
          build: () =>
            buildCanEventRows(
              eventPayload,
              fieldOrder,
              (value: LatestSignalValue) =>
                formatLatestValue(
                  value.value,
                  bySignalPath.get(`${value.source}/${value.message}.${value.signal}`),
                ),
              eventTimeMode,
            ),
        },
      }),
    [
      stateKey,
      scopeKey,
      pathsKey,
      windowEndS,
      live,
      framePayload,
      frameSignals,
      frameTimeMode,
      eventPayload,
      fieldOrder,
      bySignalPath,
      eventTimeMode,
    ],
  );

  const waiting = (params: PanelSubscribeParams | null, frame: typeof frames) =>
    params !== null && (frame === null || frame.isLoading);

  return {
    rows,
    totalRows: frames?.totalRows ?? 0,
    cap,
    // A panel already showing latched rows decides for itself that a refetch is not a loading state.
    isLoading: rows.length === 0 && (waiting(frameParams, frames) || waiting(eventParams, events)),
    error: frames?.error ?? events?.error ?? null,
  };
}
