import type { ColumnMetadata, LatestSignalValue, QueryDataMulti } from "@zeloscloud/app-extension-sdk";
import { describe, expect, it } from "vitest";
import {
  advanceCanScope,
  type BoundSignal,
  buildCanEventRows,
  type CanPanelState,
  type CanRow,
  type CanRowCache,
  type CanScopeInputs,
  canDataBytes,
  canPathsKey,
  canRowHeight,
  compareCanMessage,
  formatArbitrationId,
  mergeCanRows,
  reduceCanFrameRows,
} from "../data";

/**
 * What's asserted here is what makes the panel a bus monitor: the reduce to one row per id, the latch that
 * keeps a row alive across polls that no longer carry it, and the scope step that decides when the latch
 * is wrong.
 */

function col(over: Partial<ColumnMetadata> & { signal: string }): ColumnMetadata {
  return {
    source: "CAN",
    message: "can0/Frame",
    producer: null,
    tracePath: null,
    dataSegmentId: null,
    startTimeS: null,
    endTimeS: null,
    ...over,
  };
}

const TIME_COL = col({ source: "time_s", message: "", signal: "" });

const FRAME_SIGNALS = ["arbitration_id", "is_extended", "dlc", "data"].map(
  (signal) => ({ source: "CAN", message: "can0/Frame", signal, color: "" }) as unknown as BoundSignal,
);

function dataset(columns: ColumnMetadata[], data: unknown[][]): QueryDataMulti {
  return { columns, data } as unknown as QueryDataMulti;
}

/** Three descending frames: id 0x064 twice (newest first) and one extended id. */
function frameWindow(): QueryDataMulti {
  return dataset(
    [
      TIME_COL,
      col({ signal: "arbitration_id" }),
      col({ signal: "is_extended" }),
      col({ signal: "dlc" }),
      col({ signal: "data" }),
    ],
    [
      [30, 20, 10],
      [100, 0x1fffffff, 100],
      [false, true, false],
      [4, 2, 4],
      ["0x00010203", "0xabcd", "0x00010200"],
    ],
  );
}

describe("canDataBytes", () => {
  it("decodes the `0x…` string a binary cell arrives as, uppercased per byte", () => {
    expect(canDataBytes("0x00abff")).toEqual(["00", "AB", "FF"]);
  });

  // A garbled cell must read as "no data", never as plausible-looking wrong bytes.
  it("yields nothing at all for a malformed cell", () => {
    expect(canDataBytes("0xabc")).toEqual([]);
  });

  it("yields nothing for a cell that is not a hex string", () => {
    expect(canDataBytes(null)).toEqual([]);
  });
});

describe("formatArbitrationId", () => {
  // A standard and an extended frame may carry the same numeric id and are different frames.
  it("widths the id by its own addressing, so 11-bit and 29-bit read apart", () => {
    expect(formatArbitrationId(100, false)).toBe("0x064");
    expect(formatArbitrationId(100, true)).toBe("0x00000064");
  });
});

describe("reduceCanFrameRows", () => {
  it("keeps the newest row per (table, id, extended), formats the id and the bytes", () => {
    const rows = reduceCanFrameRows(frameWindow(), "desc", FRAME_SIGNALS, "absolute");

    expect(rows.map((row) => [row.message, row.timeS, row.dlc, row.tokens.join(" ")])).toEqual([
      ["0x064", 30, 4, "00 01 02 03"],
      ["0x1FFFFFFF", 20, 2, "AB CD"],
    ]);
    expect(rows[0]?.source).toBe("CAN/can0/Frame");
    expect(rows[0]?.kind).toBe("frame");
  });

  it("reduces an ascending window to the same rows", () => {
    const ascending = dataset(
      [TIME_COL, col({ signal: "arbitration_id" }), col({ signal: "dlc" }), col({ signal: "data" })],
      [
        [10, 30],
        [100, 100],
        [4, 4],
        ["0x00010200", "0x00010203"],
      ],
    );
    const rows = reduceCanFrameRows(ascending, "asc", FRAME_SIGNALS, "absolute");
    expect(rows).toHaveLength(1);
    expect(rows[0]?.timeS).toBe(30);
  });

  it("separates a standard and an extended frame sharing one numeric id", () => {
    const rows = reduceCanFrameRows(
      dataset(
        [TIME_COL, col({ signal: "arbitration_id" }), col({ signal: "is_extended" }), col({ signal: "data" })],
        [
          [20, 10],
          [100, 100],
          [true, false],
          ["0xaa", "0xbb"],
        ],
      ),
      "desc",
      FRAME_SIGNALS,
      "absolute",
    );
    expect(rows.map((row) => row.message)).toEqual(["0x00000064", "0x064"]);
  });

  // A producer restart opens a new segment for the same bus. That is the same id recurring, not a
  // second row for it — and the newest of the two is what the row shows.
  it("merges two segments of one table into one row per id, newest winning", () => {
    const segment = (id: string) => (over: Partial<ColumnMetadata> & { signal: string }) =>
      col({ dataSegmentId: id, ...over });
    const oldSeg = segment("seg-a");
    const newSeg = segment("seg-b");
    const rows = reduceCanFrameRows(
      dataset(
        [
          TIME_COL,
          oldSeg({ signal: "arbitration_id" }),
          oldSeg({ signal: "data" }),
          newSeg({ signal: "arbitration_id" }),
          newSeg({ signal: "data" }),
        ],
        [
          [30, 10],
          [null, 100],
          [null, "0xbb"],
          [100, null],
          ["0xaa", null],
        ],
      ),
      "desc",
      FRAME_SIGNALS,
      "absolute",
    );
    expect(rows.map((row) => [row.message, row.timeS, row.tokens.join(" ")])).toEqual([["0x064", 30, "AA"]]);
  });

  it("melts nothing from a stream without an arbitration id, and nothing from an empty window", () => {
    const noId = dataset([TIME_COL, col({ signal: "dlc" })], [[10], [4]]);
    expect(reduceCanFrameRows(noId, "desc", FRAME_SIGNALS, "absolute")).toEqual([]);
    expect(reduceCanFrameRows(null, "desc", FRAME_SIGNALS, "absolute")).toEqual([]);
  });
});

describe("buildCanEventRows", () => {
  const value = (over: Partial<LatestSignalValue> & { signal: string }): LatestSignalValue =>
    ({
      source: "CAN",
      message: "can0/012c_DUT_Logging/LOGGING_MUX_ZERO",
      producer: null,
      tracePath: null,
      dataSegmentId: null,
      timeNs: "2000000000",
      value: "1",
      ...over,
    }) as LatestSignalValue;

  it("makes one row per table, fields in bound order, a mux variant named under its message, timed by its newest field", () => {
    const order = new Map([
      ["CAN/can0/012c_DUT_Logging/LOGGING_MUX_ZERO.b", 0],
      ["CAN/can0/012c_DUT_Logging/LOGGING_MUX_ZERO.a", 1],
    ]);
    const rows = buildCanEventRows(
      [value({ signal: "a", value: "7" }), value({ signal: "b", value: "8", timeNs: "3000000000" })],
      order,
      (v) => v.value,
      "absolute",
    );

    expect(rows).toHaveLength(1);
    // The Message cell is the message and its mux variant; the full path stays on the row, for the hover text.
    expect(rows[0]?.message).toBe("012c_DUT_Logging.LOGGING_MUX_ZERO");
    expect(rows[0]?.source).toBe("CAN/can0/012c_DUT_Logging/LOGGING_MUX_ZERO");
    expect(rows[0]?.tokens).toEqual(["b = 8", "a = 7"]);
    expect(rows[0]?.dlc).toBeNull();
    expect(rows[0]?.timeS).toBe(3);
  });

  it("names a base message, and an event outside the CAN naming, by its last segment", () => {
    const rows = buildCanEventRows(
      [
        value({ signal: "a", message: "can0/012c_DUT_Logging" }),
        value({ signal: "a", message: "can0/00000100_Ext" }),
        value({ signal: "a", message: "other/status" }),
      ],
      new Map(),
      (v) => v.value,
      "absolute",
    );
    expect(rows.map((r) => r.message).sort()).toEqual(["00000100_Ext", "012c_DUT_Logging", "status"]);
  });

  it("keeps two producers of one table apart", () => {
    const rows = buildCanEventRows(
      [value({ signal: "a", producer: "agent-a" }), value({ signal: "a", producer: "agent-b" })],
      new Map(),
      (v) => v.value,
      "absolute",
    );
    expect(rows).toHaveLength(2);
  });

  // The segment is NOT part of the row's identity: a producer restart is the same table, updated.
  it("folds two segments of one table into one row", () => {
    const rows = buildCanEventRows(
      [value({ signal: "a", dataSegmentId: "seg-a" }), value({ signal: "a", dataSegmentId: "seg-b" })],
      new Map(),
      (v) => v.value,
      "absolute",
    );
    expect(rows).toHaveLength(1);
  });
});

describe("canRowHeight", () => {
  const rowOf = (kind: CanRow["kind"], lines: number) => ({ kind, tokens: Array(lines).fill("a = 1") });

  // Seven lines must fit INSIDE the height the row is given, or the last ones spill past the row.
  it("gives an event row one line height per field, padded once", () => {
    expect(canRowHeight(rowOf("event", 7), 24)).toBe(7 * 16 + 6);
    expect(canRowHeight(rowOf("event", 2), 24)).toBe(2 * 16 + 6);
  });

  it("never goes under one grid row, and leaves a frame at exactly one", () => {
    expect(canRowHeight(rowOf("event", 1), 24)).toBe(24);
    expect(canRowHeight(rowOf("frame", 8), 24)).toBe(24);
  });
});

describe("compareCanMessage", () => {
  const event = (message: string) => ({ kind: "event" as const, message });
  const frame = (message: string) => ({ kind: "frame" as const, message });

  it("puts decoded messages before raw frames, whichever way the column is sorted", () => {
    expect(compareCanMessage(frame("0x064"), event("Status"), false)).toBeGreaterThan(0);
    // AG Grid negates the result of a descending sort, so the group term is already inverted here.
    expect(compareCanMessage(frame("0x064"), event("Status"), true)).toBeLessThan(0);
  });

  // Within a group the direction is AG Grid's to apply, so the result does NOT change with it.
  it("orders within a group by the message itself, in one direction only", () => {
    expect(compareCanMessage(event("Alpha"), event("Beta"), false)).toBeLessThan(0);
    expect(compareCanMessage(event("Alpha"), event("Beta"), true)).toBeLessThan(0);
    expect(compareCanMessage(frame("0x065"), frame("0x064"), false)).toBeGreaterThan(0);
  });
});

describe("mergeCanRows", () => {
  const row = (over: Partial<CanRow> & { id: string }): CanRow => ({
    kind: "frame",
    message: "0x064",
    dlc: 2,
    tokens: ["00", "01"],
    timeS: 10,
    timeMode: "absolute",
    source: "CAN/can0/Frame",
    ...over,
  });

  it("creates a row it has never seen, with no flash", () => {
    const cache: CanRowCache = new Map();
    expect(mergeCanRows(cache, [row({ id: "a" })])).toBe(true);
    expect(cache.get("a")?.flash).toBeUndefined();
  });

  it("replaces a row with a newer one and marks the tokens that moved", () => {
    const cache: CanRowCache = new Map([["a", row({ id: "a" })]]);
    expect(mergeCanRows(cache, [row({ id: "a", timeS: 11, tokens: ["00", "02"] })])).toBe(true);
    expect(cache.get("a")?.flash).toEqual([1]);
    expect(cache.get("a")?.timeS).toBe(11);
  });

  // Polls overlap, so the same window arrives repeatedly; repainting it would flash rows nothing changed.
  it("ignores a row at or behind the one it holds", () => {
    const cache: CanRowCache = new Map([["a", row({ id: "a", timeS: 11 })]]);
    expect(mergeCanRows(cache, [row({ id: "a", timeS: 11, tokens: ["ff", "ff"] })])).toBe(false);
    expect(mergeCanRows(cache, [row({ id: "a", timeS: 5, tokens: ["ff", "ff"] })])).toBe(false);
    expect(cache.get("a")?.tokens).toEqual(["00", "01"]);
  });

  // `replaceAny` is the value-at-the-cursor rule: scrubbing back moves the reading BACK, not nowhere.
  it("replaces backwards under replaceAny, and still refuses an identical row", () => {
    const cache: CanRowCache = new Map([["a", row({ id: "a", timeS: 11 })]]);
    expect(mergeCanRows(cache, [row({ id: "a", timeS: 5, tokens: ["ff", "ff"] })], true)).toBe(true);
    expect(cache.get("a")?.timeS).toBe(5);
    expect(mergeCanRows(cache, [row({ id: "a", timeS: 5, tokens: ["ff", "ff"] })], true)).toBe(false);
  });

  it("leaves a key this poll did not carry exactly where it was", () => {
    const cache: CanRowCache = new Map([["a", row({ id: "a" })]]);
    mergeCanRows(cache, [row({ id: "b", timeS: 12 })]);
    expect([...cache.keys()]).toEqual(["a", "b"]);
  });
});

describe("advanceCanScope", () => {
  const state = (): CanPanelState => ({
    rows: new Map(),
    scope: null,
    clearedWith: { frames: null, events: null },
    seekGate: { frames: null, events: null },
    snapshot: [],
    deepReadDone: false,
  });

  const frameRow = (timeS: number): CanRow => ({
    id: "a",
    kind: "frame",
    message: "0x064",
    dlc: 1,
    tokens: [`${timeS}`],
    timeS,
    timeMode: "absolute",
    source: "CAN/can0/Frame",
  });

  const inputs = (over: Partial<CanScopeInputs> = {}): CanScopeInputs => ({
    scopeKey: "TRACE\0/a.trz",
    pathsKey: canPathsKey(["CAN/can0/Frame.data"]),
    windowEndS: 100,
    live: false,
    frames: { payload: null, build: () => [] },
    events: { payload: null, build: () => [] },
    ...over,
  });

  /** One poll carrying a fresh frames payload of `rows`. */
  const poll = (payload: unknown, rows: CanRow[], over: Partial<CanScopeInputs> = {}) =>
    inputs({ frames: { payload, build: () => rows }, ...over });

  it("latches a payload once and keeps its rows across a poll that carries nothing", () => {
    const s = state();
    expect(advanceCanScope(s, poll({}, [frameRow(10)]))).toHaveLength(1);
    expect(advanceCanScope(s, inputs())).toHaveLength(1);
  });

  it("drops the rows when a bound signal leaves, and keeps them when one is added", () => {
    const s = state();
    advanceCanScope(s, poll({}, [frameRow(10)]));
    const added = canPathsKey(["CAN/can0/Frame.data", "CAN/can0/Status.soc"]);
    expect(advanceCanScope(s, inputs({ pathsKey: added }))).toHaveLength(1);
    expect(advanceCanScope(s, inputs({ pathsKey: canPathsKey(["CAN/can0/Status.soc"]) }))).toHaveLength(0);
  });

  it("drops the rows when the workspace opens another recording", () => {
    const s = state();
    advanceCanScope(s, poll({}, [frameRow(10)]));
    expect(advanceCanScope(s, inputs({ scopeKey: "TRACE\0/b.trz" }))).toHaveLength(0);
  });

  it("drops the rows when the cursor moves back, between two cursor-addressed windows", () => {
    const s = state();
    advanceCanScope(s, poll({}, [frameRow(10)]));
    expect(advanceCanScope(s, inputs({ windowEndS: 50 }))).toHaveLength(0);
  });

  const eventRow = (timeS: number): CanRow => ({
    id: "e",
    kind: "event",
    message: "Status",
    dlc: null,
    tokens: [`soc = ${timeS}`],
    timeS,
    timeMode: "absolute",
    source: "CAN/can0/Status",
  });

  // The two feeds answer a seek at their own speeds: the decoded values are a point query that lands
  // within ~50 ms, the frame window ends at a cursor held to the panel's 200 ms poll. So the NEW event
  // payload can be latched before the frame end has retreated at all, and the clear that follows must
  // not blacklist it — a payload identity is only stale evidence when it predates the seek.
  it("keeps decoded rows that already answered the seek the clear is still catching up to", () => {
    const s = state();
    const eventAt100 = {};
    const eventAt50 = {};
    const frameAt100 = {};
    const frameAt50 = {};
    const at = (windowEndS: number, frames: unknown, frameRows: CanRow[], events: unknown, eventRows: CanRow[]) =>
      inputs({
        windowEndS,
        frames: { payload: frames, build: () => frameRows },
        events: { payload: events, build: () => eventRows },
      });

    advanceCanScope(s, at(100, frameAt100, [frameRow(100)], eventAt100, [eventRow(100)]));
    // The point query has already moved to the seek target; the throttled frame end has not.
    advanceCanScope(s, at(100, frameAt100, [frameRow(100)], eventAt50, [eventRow(50)]));
    // Now the frame end retreats and the latch clears.
    advanceCanScope(s, at(50, frameAt100, [frameRow(100)], eventAt50, [eventRow(50)]));
    // The new frame window lands. The decoded row must come back with it.
    const rows = advanceCanScope(s, at(50, frameAt50, [frameRow(50)], eventAt50, [eventRow(50)]));
    expect(rows.map((r) => r.kind).sort()).toEqual(["event", "frame"]);
  });

  // PAUSE sets the view to the end of the DATA, which is earlier than the now tick it was following.
  // That is not a seek, and wiping the latch there would empty the panel every time playback stopped.
  it("keeps the rows across LIVE → PAUSED, whose end retreats by definition", () => {
    const s = state();
    advanceCanScope(s, poll({}, [frameRow(10)], { live: true, windowEndS: 1_000 }));
    expect(advanceCanScope(s, inputs({ live: false, windowEndS: 900 }))).toHaveLength(1);
  });

  // A seek back to C' admits a payload only when its newest row is at or before C'. Payload identity says
  // nothing: the window for C' may land before or after the retreat is noticed.
  it("refills from the new window when it lands after the seek", () => {
    const s = state();
    const oldWindow = {};
    advanceCanScope(s, poll(oldWindow, [frameRow(90)]));
    // The retreat is noticed while the old window is still the payload in hand.
    expect(advanceCanScope(s, poll(oldWindow, [frameRow(90)], { windowEndS: 50 }))).toHaveLength(0);
    expect(advanceCanScope(s, poll({}, [frameRow(40)], { windowEndS: 50 })).map((r) => r.timeS)).toEqual([40]);
  });

  it("refills from the new window when it landed before the seek was noticed", () => {
    const s = state();
    advanceCanScope(s, poll({}, [frameRow(90)]));
    // The new window is already in hand: older than the latched row, so it does not move it yet.
    const newWindow = {};
    expect(advanceCanScope(s, poll(newWindow, [frameRow(40)])).map((r) => r.timeS)).toEqual([90]);
    // The cursor's retreat lands; the same payload is the answer for it, and nothing else will arrive.
    expect(advanceCanScope(s, poll(newWindow, [frameRow(40)], { windowEndS: 50 })).map((r) => r.timeS)).toEqual([
      40,
    ]);
  });

  it("ignores a payload newer than the cursor after a seek, and latches the next one inside the window", () => {
    const s = state();
    advanceCanScope(s, poll({}, [frameRow(10)]));
    expect(advanceCanScope(s, poll({}, [frameRow(80)], { windowEndS: 50 }))).toHaveLength(0);
    expect(advanceCanScope(s, poll({}, [frameRow(80)], { windowEndS: 50 }))).toHaveLength(0);
    expect(advanceCanScope(s, poll({}, [frameRow(45)], { windowEndS: 50 })).map((r) => r.timeS)).toEqual([45]);
    // The gate is lifted: the cursor moving forward latches forward as always.
    expect(advanceCanScope(s, poll({}, [frameRow(70)], { windowEndS: 75 })).map((r) => r.timeS)).toEqual([70]);
  });

  it("gates the decoded feed by the same rule", () => {
    const s = state();
    const events = (payload: unknown, rows: CanRow[], windowEndS: number) =>
      inputs({ windowEndS, events: { payload, build: () => rows } });
    advanceCanScope(s, events({}, [eventRow(90)], 100));
    expect(advanceCanScope(s, events({}, [eventRow(90)], 50))).toHaveLength(0);
    expect(advanceCanScope(s, events({}, [eventRow(48)], 50)).map((r) => r.timeS)).toEqual([48]);
  });

  // A bound path leaving makes the payload in hand one for the old binding; its replacement is a new one.
  it("ignores the payload it was cleared for when a path leaves, and latches the one that replaces it", () => {
    const s = state();
    const stale = { window: "stale" };
    advanceCanScope(s, poll(stale, [frameRow(10)]));
    const fewer = canPathsKey(["CAN/can0/Frame.dlc"]);
    expect(advanceCanScope(s, poll(stale, [frameRow(10)], { pathsKey: fewer }))).toHaveLength(0);
    expect(advanceCanScope(s, poll({ window: "fresh" }, [frameRow(11)], { pathsKey: fewer }))).toHaveLength(1);
  });
});
