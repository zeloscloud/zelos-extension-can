import { beforeEach, describe, expect, it } from "vitest";
import { type CanRow, type CanScopeInputs, canPathsKey } from "../data";
import { advanceLatch, forgetLatches, LATCH_MAX_ROWS, latchKey, markDeepReadDone, panelLatch } from "../latch";

/** sessionStorage as the latch sees it, without a browser. */
class MemoryStorage implements Storage {
  private readonly items = new Map<string, string>();
  get length(): number {
    return this.items.size;
  }
  clear(): void {
    this.items.clear();
  }
  getItem(key: string): string | null {
    return this.items.get(key) ?? null;
  }
  key(index: number): string | null {
    return [...this.items.keys()][index] ?? null;
  }
  removeItem(key: string): void {
    this.items.delete(key);
  }
  setItem(key: string, value: string): void {
    this.items.set(key, value);
  }
}

const frameRow = (id: string, timeS: number): CanRow => ({
  id,
  kind: "frame",
  message: "0x064",
  dlc: 1,
  tokens: [`${timeS}`],
  timeS,
  timeMode: "absolute",
  source: "CAN/can0/Frame",
});

const FRAME_PATHS = canPathsKey(["CAN/can0/Frame.arbitration_id", "CAN/can0/Frame.data"]);

const poll = (rows: CanRow[], over: Partial<CanScopeInputs> = {}): CanScopeInputs => ({
  scopeKey: "ws-1\0TRACE",
  pathsKey: FRAME_PATHS,
  windowEndS: 100,
  live: false,
  frames: { payload: rows.length > 0 ? {} : null, build: () => rows },
  events: { payload: null, build: () => [] },
  ...over,
});

let storage: MemoryStorage;

beforeEach(() => {
  forgetLatches();
  storage = new MemoryStorage();
});

describe("latch", () => {
  it("persists every merge under the panel instance's key", () => {
    advanceLatch("p1", poll([frameRow("a", 10)]), storage);
    const stored = JSON.parse(storage.getItem(latchKey("p1")) ?? "null");
    expect(latchKey("p1")).toBe("zelos.panel.p1.latch");
    expect(stored.rows.map((row: CanRow) => row.id)).toEqual(["a"]);
    expect(stored.scope.endS).toBe(100);

    advanceLatch("p1", poll([frameRow("b", 20)]), storage);
    const again = JSON.parse(storage.getItem(latchKey("p1")) ?? "null");
    expect(again.rows.map((row: CanRow) => row.id)).toEqual(["a", "b"]);
  });

  // A reload empties the document's memory; the rows must come back from the store, without a flash.
  it("restores the rows, the scope and the deep-read flag on load", () => {
    advanceLatch("p1", poll([frameRow("a", 10)]), storage);
    advanceLatch("p1", poll([{ ...frameRow("a", 11), tokens: ["ff"] }]), storage);
    markDeepReadDone("p1", storage);
    forgetLatches();

    const restored = panelLatch("p1", storage);
    expect(restored.snapshot.map((row) => row.id)).toEqual(["a"]);
    expect(restored.snapshot[0]?.flash).toBeUndefined();
    expect(restored.deepReadDone).toBe(true);
    expect(restored.scope?.endS).toBe(100);
    // A poll carrying nothing new keeps what was restored.
    expect(advanceLatch("p1", poll([]), storage)).toHaveLength(1);
  });

  it("stores at most the newest LATCH_MAX_ROWS rows", () => {
    const rows = Array.from({ length: LATCH_MAX_ROWS + 10 }, (_, i) => frameRow(`r${i}`, i));
    expect(advanceLatch("p1", poll(rows), storage)).toHaveLength(LATCH_MAX_ROWS + 10);

    const stored = JSON.parse(storage.getItem(latchKey("p1")) ?? "null");
    expect(stored.rows).toHaveLength(LATCH_MAX_ROWS);
    expect(stored.rows[0].id).toBe("r10");
    expect(stored.rows.at(-1).id).toBe(`r${LATCH_MAX_ROWS + 9}`);
  });

  it("clears, in memory and in the store, when a bound signal leaves", () => {
    advanceLatch("p1", poll([frameRow("a", 10)]), storage);
    const fewer = canPathsKey(["CAN/can0/Frame.data"]);
    expect(advanceLatch("p1", poll([], { pathsKey: fewer }), storage)).toHaveLength(0);
    expect(JSON.parse(storage.getItem(latchKey("p1")) ?? "null").rows).toEqual([]);
  });

  // The set changed while the panel was not loaded: the restored scope still names the old paths.
  it("clears a restored latch whose signal set no longer matches", () => {
    advanceLatch("p1", poll([frameRow("a", 10)]), storage);
    forgetLatches();
    expect(advanceLatch("p1", poll([], { pathsKey: canPathsKey(["CAN/can1/Frame.data"]) }), storage)).toHaveLength(0);
  });

  it("keeps two panel instances apart", () => {
    advanceLatch("p1", poll([frameRow("a", 10)]), storage);
    expect(panelLatch("p2", storage).snapshot).toHaveLength(0);
  });

  it("keeps working when the store refuses writes", () => {
    const full = new MemoryStorage();
    full.setItem = () => {
      throw new Error("QuotaExceededError");
    };
    expect(advanceLatch("p1", poll([frameRow("a", 10)]), full)).toHaveLength(1);
  });
});
