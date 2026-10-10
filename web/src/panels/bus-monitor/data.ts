import type {
  AppBridgePanelSignal,
  ColumnMetadata,
  LatestSignalValue,
  QueryCellValue,
  QueryDataMulti,
  SortOrder,
} from "@zeloscloud/app-extension-sdk";
import { parseFrameHex } from "./hex-bytes";
import { compareNaturalMixed } from "./natural-compare";
import { nsToSec, type TimeMode } from "./time";

/**
 * The Bus Monitor is a LATCH, not a list: one row per thing on the bus, updated in place as it recurs.
 * Two sources feed it and both reduce to the same row.
 *
 *  - a raw frame table (`zelos.can.frame.*`) fans out to one row per (table, arbitration id, extended
 *    flag) — the bus carries many ids through one table, so the window has to be reduced per id;
 *  - anything else — a decoded message table, a log, a field dragged on its own — is one row per table,
 *    read from the host's latest-value subscription.
 *
 * Nothing here parses a bus out of a path: a row's identity is the backend table it came from, exactly
 * as the catalog names it, plus the id within it.
 */

/** What the panel reads of a bound signal: its path parts, its scope, and the event type it belongs to. */
export type BoundSignal = Pick<
  AppBridgePanelSignal,
  "source" | "message" | "signal" | "producer" | "tracePath" | "dataSegmentId" | "eventType"
>;

/** Raw CAN frames. Prefix, not an exact id, so a version bump routes and melts the same way. */
const CAN_FRAME_EVENT_PREFIX = "zelos.can.frame.";

function isCanFrameSignal(signal: { eventType?: string | null | undefined }): boolean {
  return (signal.eventType ?? "").startsWith(CAN_FRAME_EVENT_PREFIX);
}

/** The `zelos.can.frame.v1` fields the panel reads. `is_fd`/`is_rx` are not columns, so not projected. */
type CanFrameField = "arbitration_id" | "is_extended" | "dlc" | "data";

const CAN_FRAME_FIELDS: readonly CanFrameField[] = ["arbitration_id", "is_extended", "dlc", "data"];

const CAN_FRAME_FIELD_SET: ReadonlySet<string> = new Set(CAN_FRAME_FIELDS);

/** The backend table a signal belongs to, as the catalog names it. */
function tablePath(signal: { source: string; message: string }): string {
  return `${signal.source}/${signal.message}`;
}

/** The bound frame signals the window projects — the four fields above, out of the six an event drop binds. */
export function canFrameQuerySignals<S extends BoundSignal>(signals: readonly S[]): S[] {
  return signals.filter((signal) => isCanFrameSignal(signal) && CAN_FRAME_FIELD_SET.has(signal.signal));
}

/**
 * Everything the frame window does NOT already show, read from the latest-value subscription instead.
 *
 * A frame field the window has no column for (`is_fd` dragged on its own) would otherwise bind and render
 * nothing; here it becomes a value row. Once any field of that table IS projected the frame rows speak for
 * it, so its unprojected siblings stay out rather than adding a second row for the same table.
 */
export function canEventQuerySignals<S extends BoundSignal>(signals: readonly S[]): S[] {
  const framed = new Set(canFrameQuerySignals(signals).map(tablePath));
  return signals.filter((signal) => !isCanFrameSignal(signal) || !framed.has(tablePath(signal)));
}

// --- Column ownership and grouping --------------------------------------------------------------

/** A scope axis the bound signal leaves unset admits every value on it. */
function scopeAdmitsColumn(column: ColumnMetadata, signal: BoundSignal): boolean {
  if (signal.producer !== undefined && (column.producer ?? null) !== signal.producer) return false;
  if (signal.tracePath !== undefined && (column.tracePath ?? null) !== signal.tracePath) return false;
  if (signal.dataSegmentId && signal.dataSegmentId !== (column.dataSegmentId ?? null)) return false;
  return true;
}

/**
 * Does the panel still bind this column? The window is wildcard-by-path and returns EVERY scope of a bound
 * path — including scopes the user has since removed — so the bound signals are the only source of truth.
 * An empty list means "no membership to check".
 */
function isColumnOwnedByPanel(column: ColumnMetadata, signals: readonly BoundSignal[]): boolean {
  if (signals.length === 0) return true;
  return signals.some(
    (signal) =>
      column.source === signal.source &&
      column.message === signal.message &&
      column.signal === signal.signal &&
      scopeAdmitsColumn(column, signal),
  );
}

/**
 * One bound stream — one backend TABLE. The query registers a table per `(agent, segment, source, event)`
 * and aliases its columns fully-qualified, so each stream owns a disjoint set of column indices.
 */
interface FieldStream {
  producer: string | null;
  tracePath: string | null;
  /** The stream's catalog coordinates — what names a signal path back to this same table. */
  source: string;
  message: string;
  /** Column index per field, or -1 when this stream doesn't carry that field. */
  fields: Record<CanFrameField, number>;
}

/** A column's stream identity. The segment is part of it: concurrent segments are distinct tables. */
function streamKeyOf(column: ColumnMetadata): string {
  return [column.tracePath, column.producer, column.dataSegmentId, column.source, column.message]
    .map((part) => part ?? "")
    .join("\u0000");
}

/**
 * Group a window's columns into the streams they came from. Columns outside the frame fields are ignored and
 * columns the panel no longer binds are dropped; a field the window didn't project keeps its `-1`.
 */
function buildFieldStreams(
  columns: readonly ColumnMetadata[],
  timeColumnIndex: number,
  panelSignals: readonly BoundSignal[],
): FieldStream[] {
  const streams = new Map<string, FieldStream>();

  for (let i = 0; i < columns.length; i++) {
    const column = columns[i];
    if (i === timeColumnIndex || !column || !CAN_FRAME_FIELD_SET.has(column.signal)) continue;
    if (!isColumnOwnedByPanel(column, panelSignals)) continue;

    const key = streamKeyOf(column);
    let stream = streams.get(key);
    if (!stream) {
      stream = {
        producer: column.producer,
        tracePath: column.tracePath,
        source: column.source,
        message: column.message,
        fields: { arbitration_id: -1, is_extended: -1, dlc: -1, data: -1 },
      };
      streams.set(key, stream);
    }
    stream.fields[column.signal as CanFrameField] = i;
  }

  return [...streams.values()];
}

// --- Rows ----------------------------------------------------------------------------------------

export type CanRowKind = "frame" | "event";

/** One latched row. Every column is resolved at build time; the grid formats nothing but the time. */
export interface CanRow {
  /** Cache key: the backend table, plus the arbitration id within it for a frame row. */
  id: string;
  kind: CanRowKind;
  /** Frames: the id as hex. Events: the LAST segment of the event's name — {@link source} keeps the path. */
  message: string;
  /** The frame's DLC. `null` for an event: a decoded message has no byte count, and its field count
   *  is not one. */
  dlc: number | null;
  /** The Data cell, one token per byte (frames) or per `signal = value` line (events). */
  tokens: readonly string[];
  timeS: number;
  /**
   * What `timeS` MEANS — epoch seconds (`absolute`) or elapsed seconds (`relative`). Stamped per row:
   * the two feeds resolve their window separately and can disagree for a frame after a time-mode switch.
   */
  timeMode: TimeMode;
  /** The table this row came from, as the catalog names it (`CAN/can0/Frame`). */
  source: string;
  /** Token indexes that differ from the row this one replaced — exactly what the flash paints. */
  flash?: readonly number[] | undefined;
}

/**
 * `0x064` / `0x1FFFFFFF`. The width follows the id's own addressing (11-bit or 29-bit) rather than the
 * value, so `0x064` and `0x00000064` read as the two different frames they are.
 */
export function formatArbitrationId(id: number, isExtended: boolean): string {
  const digits = isExtended ? 8 : 3;
  return `0x${id.toString(16).toUpperCase().padStart(digits, "0")}`;
}

/** `0x0011ff` → `["00", "11", "FF"]`. */
export function canDataBytes(value: QueryCellValue): string[] {
  if (typeof value !== "string") return [];
  return Array.from(parseFrameHex(value), (byte) => byte.toString(16).toUpperCase().padStart(2, "0"));
}

/** The Data cell as TEXT — bytes spaced, decoded fields one per line. What "Copy value" copies. */
export function canDataText(row: Pick<CanRow, "kind" | "tokens">): string {
  return row.tokens.join(row.kind === "frame" ? " " : "\n");
}

// --- Row layout ----------------------------------------------------------------------------------

/**
 * An event row's Data cell: one step under the grid's value size and single-spaced, so seven fields read
 * as a block rather than as seven grid rows.
 *
 * The class's line height IS {@link CAN_EVENT_LINE_PX} — the height below is computed from the px and the
 * cell renders the class, so the two must move together or a row is taller or shorter than what it holds.
 */
export const CAN_EVENT_LINE_CLASS = "can-event-lines";
const CAN_EVENT_LINE_PX = 16;

/** The cell's border and breathing room, counted ONCE for the whole stack rather than once per line. */
const CAN_EVENT_PADDING_PX = 6;

/** A row's height in px: one grid row for a frame, its own lines plus one padding for an event. */
export function canRowHeight(row: Pick<CanRow, "kind" | "tokens">, gridRowHeight: number): number {
  if (row.kind === "frame") return gridRowHeight;
  return Math.max(gridRowHeight, row.tokens.length * CAN_EVENT_LINE_PX + CAN_EVENT_PADDING_PX);
}

/** Decoded messages first, raw frames last. */
const CAN_KIND_RANK: Record<CanRowKind, number> = { event: 0, frame: 1 };

function kindRank(row: Pick<CanRow, "kind"> | undefined): number {
  return row ? CAN_KIND_RANK[row.kind] : 0;
}

/**
 * The Message column's order: the decoded messages as a block, the raw frames as another, each group in
 * the grid's own natural order.
 *
 * `descending` is applied to the GROUP term here and nowhere else, because AG Grid negates the whole
 * comparator result — left alone, toggling the sort direction would put the frames on top.
 */
export function compareCanMessage(
  a: Pick<CanRow, "kind" | "message"> | undefined,
  b: Pick<CanRow, "kind" | "message"> | undefined,
  descending: boolean,
): number {
  const group = kindRank(a) - kindRank(b);
  if (group !== 0) return descending ? -group : group;
  return compareNaturalMixed(a?.message, b?.message);
}

/**
 * A row's identity: the table it came from, WITHOUT the data segment. A producer restart opens a new
 * segment for the same bus, and that is the same id recurring — not a second row for it.
 */
function rowTableKey(table: { tracePath: string | null; producer: string | null; source: string; message: string }) {
  return [table.tracePath, table.producer, table.source, table.message].map((part) => part ?? "").join("\0");
}

function cellAt(data: readonly QueryCellValue[][], columnIndex: number, row: number): QueryCellValue {
  return columnIndex >= 0 ? (data[columnIndex]?.[row] ?? null) : null;
}

/** One frame stream plus the ids already answered for its table, shared across that table's segments. */
interface FrameStream {
  stream: FieldStream;
  tableKey: string;
  seen: Set<number>;
}

/** The window's frame streams. `arbitration_id` IS a row's identity, so a stream without it is not one. */
function frameStreamsOf(
  columns: readonly ColumnMetadata[],
  timeColumnIndex: number,
  panelSignals: readonly BoundSignal[],
): FrameStream[] {
  // Two segments of one bus answer for the same ids, so they share a probe set.
  const seenByTable = new Map<string, Set<number>>();
  return buildFieldStreams(columns, timeColumnIndex, panelSignals)
    .filter((stream) => stream.fields.arbitration_id >= 0)
    .map((stream) => {
      const tableKey = rowTableKey(stream);
      const seen = seenByTable.get(tableKey) ?? new Set<number>();
      seenByTable.set(tableKey, seen);
      return { stream, tableKey, seen };
    });
}

/** Window row `i` as a frame row of `frameStream`, or null when it belongs to another stream or its id is
 *  already answered by a newer row. */
function frameRowAt(
  data: readonly QueryCellValue[][],
  i: number,
  timeS: number,
  timeMode: TimeMode,
  { stream, tableKey, seen }: FrameStream,
): CanRow | null {
  const arbitrationId = cellAt(data, stream.fields.arbitration_id, i);
  // The union is sparse: a row belongs to exactly one stream, and the rest read null here.
  if (typeof arbitrationId !== "number") return null;

  const isExtended = cellAt(data, stream.fields.is_extended, i) === true;
  // The probe is a number, so the 10k rows a poll discards cost no string. The id string below is
  // built only for the handful of rows that are kept.
  const probe = arbitrationId * 2 + (isExtended ? 1 : 0);
  if (seen.has(probe)) return null;
  seen.add(probe);

  const tokens = canDataBytes(cellAt(data, stream.fields.data, i));
  const dlc = cellAt(data, stream.fields.dlc, i);
  return {
    id: `${tableKey}\0${probe}`,
    kind: "frame",
    message: formatArbitrationId(arbitrationId, isExtended),
    dlc: typeof dlc === "number" ? dlc : tokens.length,
    tokens,
    timeS,
    timeMode,
    source: tablePath(stream),
  };
}

export function reduceCanFrameRows(
  dataset: QueryDataMulti | null | undefined,
  fetchedOrder: SortOrder,
  panelSignals: readonly BoundSignal[],
  timeMode: TimeMode,
): CanRow[] {
  if (!dataset) return [];
  const { columns, data } = dataset;
  // The time column is identified by metadata, never by position; a window without one has no rows.
  const timeColumnIndex = columns.findIndex((column) => column.source === "time_s");
  const timeColumn = data[timeColumnIndex] ?? [];
  const frameStreams = frameStreamsOf(columns, timeColumnIndex, panelSignals);
  if (frameStreams.length === 0) return [];

  const rows: CanRow[] = [];
  for (let step = 0; step < timeColumn.length; step++) {
    const i = fetchedOrder === "desc" ? step : timeColumn.length - 1 - step;
    const timeS = Number(timeColumn[i]);
    if (!Number.isFinite(timeS)) continue;
    for (const frameStream of frameStreams) {
      const row = frameRowAt(data, i, timeS, timeMode, frameStream);
      if (row) rows.push(row);
    }
  }
  return rows;
}

/** A decoded CAN message's event name: `{id}_{Message}`, the id 4 or 8 hex digits wide. */
const CAN_MESSAGE_EVENT = /^(?:[0-9a-f]{4}|[0-9a-f]{8})_/;

/**
 * `vcan0/0100_BMS_BatteryStatus` → `0100_BMS_BatteryStatus`. A mux variant nests under its message, so it
 * keeps it: `vcan0/04e0_BMU00_InfoMessage/SERIAL` → `04e0_BMU00_InfoMessage.SERIAL`. The bus stays on the
 * tooltip.
 */
function messageName(path: string): string {
  const parts = path.split("/");
  const name = parts.at(-1) ?? path;
  const parent = parts.at(-2);
  return parent !== undefined && CAN_MESSAGE_EVENT.test(parent) ? `${parent}.${name}` : name;
}

/**
 * Latest values → one row per table, its fields on their own lines.
 *
 * `fieldOrder` is the order the panel bound the fields in, i.e. the event's own field order at the drag;
 * a field the panel doesn't know sorts last, by name, rather than jumping the schema.
 */
export function buildCanEventRows(
  values: readonly LatestSignalValue[] | null | undefined,
  fieldOrder: ReadonlyMap<string, number>,
  formatValue: (value: LatestSignalValue) => string,
  timeMode: TimeMode,
): CanRow[] {
  if (!values?.length) return [];

  const groups = new Map<string, { row: CanRow; fields: { name: string; text: string; order: number }[] }>();

  for (const value of values) {
    const key = rowTableKey({
      tracePath: value.tracePath ?? null,
      producer: value.producer ?? null,
      source: value.source,
      message: value.message,
    });
    let group = groups.get(key);
    if (!group) {
      group = {
        row: {
          id: key,
          kind: "event",
          message: messageName(value.message),
          dlc: null,
          tokens: [],
          timeS: 0,
          timeMode,
          source: tablePath(value),
        },
        fields: [],
      };
      groups.set(key, group);
    }
    const path = `${value.source}/${value.message}.${value.signal}`;
    group.fields.push({
      name: value.signal,
      text: formatValue(value),
      order: fieldOrder.get(path) ?? Number.MAX_SAFE_INTEGER,
    });
    // Every field of one emit shares an instant; `max` is what survives a table whose fields last
    // changed at different times.
    const timeS = nsToSec(value.timeNs);
    if (Number.isFinite(timeS) && timeS > group.row.timeS) group.row.timeS = timeS;
  }

  return [...groups.values()].map(({ row, fields }) => {
    fields.sort((a, b) => a.order - b.order || a.name.localeCompare(b.name));
    return { ...row, tokens: fields.map((field) => `${field.name} = ${field.text}`) };
  });
}

/** Token indexes whose text moved; `undefined` when none did, so an unchanged row carries no flash. */
function changedTokens(previous: readonly string[], next: readonly string[]): number[] | undefined {
  const changed: number[] = [];
  for (let i = 0; i < next.length; i++) {
    if (next[i] !== previous[i]) changed.push(i);
  }
  return changed.length > 0 ? changed : undefined;
}

function sameCanRow(a: CanRow, b: CanRow): boolean {
  return (
    a.timeS === b.timeS && a.tokens.length === b.tokens.length && a.tokens.every((token, i) => token === b.tokens[i])
  );
}

/** The latched rows of one panel, keyed by {@link CanRow.id}. Insertion order is first-seen order. */
export type CanRowCache = Map<string, CanRow>;

/**
 * Latch `incoming` into `cache`, reporting whether anything moved.
 *
 * A key first seen is created. A key whose newest row moved FORWARD is replaced, carrying the tokens that
 * differ so the flash can paint them. A row at or behind the one already held is ignored — polls overlap,
 * and a re-fetch of the same window must not repaint; a row arriving at the SAME instant is refused too,
 * since which of two equal-time rows a window lists first is the backend's order, not a fact about the bus.
 * A key this poll didn't carry is left alone: a row leaves only when the panel, the trace, or the signal
 * behind it does.
 *
 * `replaceAny` drops the forward-only rule: the row is then "the value at the cursor", which moves in both
 * directions, so any change to it replaces.
 */
export function mergeCanRows(cache: CanRowCache, incoming: readonly CanRow[], replaceAny = false): boolean {
  let changed = false;
  for (const row of incoming) {
    const previous = cache.get(row.id);
    if (previous && (replaceAny ? sameCanRow(previous, row) : previous.timeS >= row.timeS)) continue;
    cache.set(row.id, previous ? { ...row, flash: changedTokens(previous.tokens, row.tokens) } : row);
    changed = true;
  }
  return changed;
}

// --- The latch -----------------------------------------------------------------------------------

/** What the latched rows describe. Anything here changing means they describe something else. */
export interface CanRowScope {
  /** The workspace and the recordings it reads: another recording's rows are not these rows. */
  scopeKey: string;
  /** The signal paths the panel binds, sorted and joined; one leaving takes its rows with it. */
  pathsKey: string;
  /** Live follow, so {@link endS} below is the now tick rather than a cursor. */
  live: boolean;
  /** The newest instant asked for. A cursor-addressed end that retreats makes the rows the future. */
  endS: number;
}

/**
 * One panel's latch. The point of latching is that a row seen once stays until something says otherwise,
 * so it outlives the panel's document: `latch.ts` keeps it per panel instance and in sessionStorage.
 */
export interface CanPanelState {
  rows: CanRowCache;
  scope: CanRowScope | null;
  /** The payloads the latch was last cleared FOR because they describe something else — re-latching one
   *  would undo the clear. A subscription that changes with the bound signals answers with a new payload. */
  clearedWith: { frames: unknown; events: unknown };
  /**
   * After a seek back, the newest instant a feed's payload may hold to be the answer for the new cursor; null
   * once that feed has delivered one. The host keeps serving a subscription's last payload until the new
   * window lands, so a payload is judged by its TIMES, never by its identity: the new window may already be
   * the payload in hand when the cursor's retreat is noticed.
   */
  seekGate: { frames: number | null; events: number | null };
  /** The rows as an array, rebuilt only when the latch moves, so AG Grid can skip an unchanged poll. */
  snapshot: CanRow[];
  /** The one deep first window has landed, so a reload polls shallowly instead of paying for it again. */
  deepReadDone: boolean;
}

/** One feed's contribution: the payload to compare, and the rows to build from it if it is new. */
export interface CanFeed {
  payload: unknown;
  build: () => CanRow[];
}

/** One poll's inputs to {@link advanceCanScope}. */
export interface CanScopeInputs {
  scopeKey: string;
  pathsKey: string;
  /** The newest instant asked for: the now tick live, the cursor when paused or in a trace. */
  windowEndS: number | null;
  /** Live follow. Paused live and trace both read a cursor-addressed window instead. */
  live: boolean;
  frames: CanFeed;
  events: CanFeed;
}

/** The paths key is sorted and NUL-joined, so a bound path can't be confused with a substring of one. */
export function canPathsKey(paths: readonly string[]): string {
  return [...paths].sort().join("\0");
}

/** A path LEFT the panel. Adding one keeps what is latched — nothing said the old rows were wrong. */
function pathsDropped(previousKey: string, nextKey: string): boolean {
  if (previousKey === nextKey) return false;
  const next = new Set(nextKey.split("\0"));
  return previousKey.split("\0").some((path) => !next.has(path));
}

/**
 * The window's end retreated, so the latched rows are the future.
 *
 * Only between two cursor-addressed ends. Live's end is the now tick, and PAUSE drops it back to the end
 * of the DATA — so treating that transition as a seek would wipe the latch every time playback stopped.
 */
function windowRetreated(scope: CanRowScope, inputs: CanScopeInputs): boolean {
  if (scope.live || inputs.live || inputs.windowEndS === null) return false;
  return inputs.windowEndS < scope.endS;
}

/**
 * A feed's rows, or null when its payload must not be latched: none at all, one cleared because it describes
 * something else, or — after a seek back — one holding a row newer than the cursor it now answers for, which
 * makes it the window the cursor left. The first payload that passes the gate lifts it.
 */
function admit(state: CanPanelState, feed: "frames" | "events", input: CanFeed): CanRow[] | null {
  if (!input.payload || input.payload === state.clearedWith[feed]) return null;
  const rows = input.build();
  const gate = state.seekGate[feed];
  if (gate !== null) {
    if (rows.some((row) => row.timeS > gate)) return null;
    state.seekGate[feed] = null;
  }
  return rows;
}

/** A seek back sets both gates to the new end; live lifts them; otherwise an open gate follows the cursor. */
function nextSeekGate(
  gate: CanPanelState["seekGate"],
  inputs: CanScopeInputs,
  soughtBack: boolean,
): CanPanelState["seekGate"] {
  if (soughtBack) return { frames: inputs.windowEndS, events: inputs.windowEndS };
  if (inputs.live) return { frames: null, events: null };
  // The cursor can move on before the new window lands; the gate follows it.
  const follow = (held: number | null) => (held === null ? null : (inputs.windowEndS ?? held));
  return { frames: follow(gate.frames), events: follow(gate.events) };
}

/** The newest end asked for so far. A clear or a live↔paused switch re-bases: the two ends mean different
 *  things, so neither bounds the other. */
function nextEndS(scope: CanRowScope | null, inputs: CanScopeInputs, cleared: boolean): number {
  if (cleared || scope === null || scope.live !== inputs.live) return inputs.windowEndS ?? 0;
  return Math.max(scope.endS, inputs.windowEndS ?? scope.endS);
}

/**
 * Advance one panel's latch by one poll: clear it if it now describes something else, then merge whichever
 * feed carries a payload it hasn't already been given. Returns the rows to render.
 *
 * Pure in the sense that matters: everything it reads is in `state` and `inputs`, so the whole clear/latch
 * policy is testable without a React tree.
 */
export function advanceCanScope(state: CanPanelState, inputs: CanScopeInputs): CanRow[] {
  const scope = state.scope;
  // Kept apart because they gate payloads differently: one says these rows are not OF this panel any more,
  // the other only that they are ahead of where the cursor now points.
  const describesSomethingElse =
    scope !== null && (scope.scopeKey !== inputs.scopeKey || pathsDropped(scope.pathsKey, inputs.pathsKey));
  const soughtBack = scope !== null && windowRetreated(scope, inputs);
  const cleared = describesSomethingElse || soughtBack;

  if (cleared) state.rows.clear();
  if (describesSomethingElse) {
    state.clearedWith = { frames: inputs.frames.payload, events: inputs.events.payload };
  }
  state.seekGate = nextSeekGate(state.seekGate, inputs, soughtBack);
  state.scope = {
    scopeKey: inputs.scopeKey,
    pathsKey: inputs.pathsKey,
    live: inputs.live,
    endS: nextEndS(scope, inputs, cleared),
  };

  let changed = cleared;
  const frames = admit(state, "frames", inputs.frames);
  if (frames) changed = mergeCanRows(state.rows, frames) || changed;
  const events = admit(state, "events", inputs.events);
  // Paused, an event row IS the value at the cursor and follows it backward as well as forward.
  // Live, it latches like a frame: the newest reading wins.
  if (events) changed = mergeCanRows(state.rows, events, !inputs.live) || changed;

  if (changed) state.snapshot = [...state.rows.values()];
  return state.snapshot;
}
