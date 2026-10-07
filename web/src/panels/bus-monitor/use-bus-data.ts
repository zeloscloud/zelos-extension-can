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

/**
 * Rows the FIRST window of a panel fetches. An id that recurs slowly appears once in a deep window and
 * not at all in a shallow one, and a row never seen is never latched — so the panel pays for one deep
 * read and polls shallowly forever after. Once per panel per SESSION, not per load: the latch outlives a
 * reload, so a reload would otherwise pay for it again for rows it already holds.
 */
const CAN_FIRST_MAX_ROWS = 100_000;

interface BusMonitorData {
  rows: CanRow[];
  /** Rows the frame window held before the cap cut it, and the cap, fitted to the host's cell limit. */
  totalRows: number;
  cap: number;
  isLoading: boolean;
  error: string | null;
}

/**
 * A value held to at most one change per `ms`: the first change lands at once, later ones at the end of
 * the interval, and the final value always lands.
 */
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

/** A decimal or exponent literal: what a float column prints. An integer's text never goes through a
 *  JS number, so a 64-bit id keeps every digit. */
const FLOAT_TEXT = /^-?(?:\d+\.\d*|\.\d+|\d+(?:\.\d*)?[eE][-+]?\d+)$/;

/**
 * A float's text as every other value readout in Zelos shows it: the number's own shortest form, an
 * exponent form kept as is (so a tiny value never collapses to 0), otherwise at most 8 fraction digits.
 */
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
function valueTableLabel(value: string, table: Record<string, string> | null | undefined): string | null {
  if (!table || !Object.hasOwn(table, value)) return null;
  const label = table[value];
  return typeof label === "string" ? label : null;
}

/**
 * A latest value as text: the host's string, `-` when empty, then the catalog unit. A value-table hit reads
 * `LABEL (n)`, so the code stays visible beside its name. A dictionary-encoded signal's value is already its
 * label and is shown verbatim, as is its table label.
 */
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

/**
 * The panel's latched rows: the frame window reduced to its newest row per id, plus a row per other bound
 * table read from the host's latest-value subscription, merged into a latch that outlives every poll.
 *
 * The two feeds are deliberately different subscriptions. Frames need the WINDOW — one table carries every
 * id on the bus, so only a row scan can say what the newest frame for each id was. Everything else already
 * has a "latest row of this table" answer, and asking for a window would be a full scan to reach one row.
 *
 * Both feeds answer the same question about time. Following live, that is "the newest there is". Paused or
 * in a trace, it is "as of the CURSOR": the frame window ends there, the latest-value query is already an
 * as-of at the cursor, and scrubbing back moves both.
 */
export function useBusMonitorData(panel: AppBridgePanelInfo): BusMonitorData {
  const { instanceId, signals } = panel;
  const frameSignals = useMemo(() => canFrameQuerySignals(signals), [signals]);
  const eventSignals = useMemo(() => canEventQuerySignals(signals), [signals]);

  const workspace = useWorkspace();
  const time = useTimeState();
  const live = workspace?.modeKind === "LIVE" && time?.playback === "LIVE";
  // A cursor sweep moves with the pointer, and every position it passes through is a different window.
  // Held to the poll rate, with the final position always landing.
  const cursorS = useThrottledValue(time?.cursorS ?? null, CAN_POLL_MS);
  const viewEndS = time?.viewRange?.endS ?? time?.dataRange?.endS ?? null;
  // Live follows the tail; paused and trace end the window at the cursor. The two feeds reach it on their
  // own clocks, so during a sweep the frames can be up to one poll behind, and they converge when the
  // cursor settles.
  const windowEndS = live ? viewEndS : (cursorS ?? viewEndS);

  const [wantedRows, setWantedRows] = useState(() =>
    panelLatch(instanceId).deepReadDone ? CAN_MAX_ROWS : CAN_FIRST_MAX_ROWS,
  );
  const framePaths = useMemo(() => uniquePaths(frameSignals), [frameSignals]);
  // Many buses bound at once ask for fewer rows each rather than being refused.
  const cap = fitRowBudget(framePaths.length, wantedRows);
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
        : { id: "latest", shape: "latest", signals: uniquePaths(eventSignals), minPollMs: CAN_POLL_MS },
    [eventSignals],
  );
  const frames = usePanelData(frameParams);
  const events = usePanelData(eventParams);
  const framePayload = frames?.dataset ?? null;
  const eventPayload = events?.latest ?? null;

  // The deep read has landed and is latched; every later poll only has to carry what has changed since.
  useEffect(() => {
    if (!framePayload) return;
    markDeepReadDone(instanceId);
    setWantedRows(CAN_MAX_ROWS);
  }, [framePayload, instanceId]);

  const bySignalPath = useMemo(() => new Map(signals.map((signal) => [signal.path, signal])), [signals]);
  const fieldOrder = useMemo(() => {
    const order = new Map<string, number>();
    for (const signal of eventSignals) if (!order.has(signal.path)) order.set(signal.path, order.size);
    return order;
  }, [eventSignals]);

  // What the latched rows describe. Another workspace or mode makes them rows from elsewhere; a signal
  // leaving the panel makes them rows nothing asked for; a cursor moving back makes them the future. A
  // signal joining, from any producer or trace, keeps them.
  const scopeKey = `${workspace?.id ?? ""}\0${workspace?.modeKind ?? ""}`;
  const pathsKey = useMemo(() => canPathsKey(signals.map((signal) => signal.path)), [signals]);
  const frameTimeMode = toTimeMode(frames?.timeMode);
  const eventTimeMode = toTimeMode(events?.timeMode ?? time?.mode);

  const rows = useMemo(
    () =>
      advanceLatch(instanceId, {
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
                formatLatestValue(value.value, bySignalPath.get(`${value.source}/${value.message}.${value.signal}`)),
              eventTimeMode,
            ),
        },
      }),
    [
      instanceId,
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
