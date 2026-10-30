import {
  advanceCanScope,
  type CanPanelState,
  type CanRow,
  type CanRowScope,
  type CanScopeInputs,
} from "./data";

// The host reloads a panel on a layout tab switch, so the latch lives in sessionStorage, scope and all.

/** The most rows a stored latch keeps: the newest by time. A bus has far fewer ids than this. */
export const LATCH_MAX_ROWS = 5_000;

export function latchKey(panelKey: string): string {
  return `zelos.panel.${panelKey}.latch`;
}

interface StoredLatch {
  version: 1;
  rows: CanRow[];
  scope: CanRowScope | null;
  deepReadDone: boolean;
}

/** The document's sessionStorage, or null where the browser refuses it (blocked site data, a sandbox). */
function sessionStore(): Storage | null {
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null;
  }
}

const LATCHES = new Map<string, CanPanelState>();

function emptyLatch(): CanPanelState {
  return {
    rows: new Map(),
    scope: null,
    clearedWith: { frames: null, events: null },
    seekGate: { frames: null, events: null },
    snapshot: [],
    deepReadDone: false,
  };
}

function restore(panelKey: string, storage: Storage | null): CanPanelState {
  const state = emptyLatch();
  let stored: Partial<StoredLatch> | null;
  try {
    const raw = storage?.getItem(latchKey(panelKey));
    stored = raw ? (JSON.parse(raw) as Partial<StoredLatch>) : null;
  } catch {
    stored = null;
  }
  if (stored?.version !== 1 || !Array.isArray(stored.rows)) return state;

  for (const row of stored.rows) state.rows.set(row.id, row);
  state.scope = stored.scope ?? null;
  state.deepReadDone = stored.deepReadDone === true;
  state.snapshot = [...state.rows.values()];
  return state;
}

/** One panel instance's latch: in memory when this document already holds it, else restored from storage. */
export function panelLatch(
  panelKey: string,
  storage: Storage | null = sessionStore(),
): CanPanelState {
  let state = LATCHES.get(panelKey);
  if (!state) {
    state = restore(panelKey, storage);
    LATCHES.set(panelKey, state);
  }
  return state;
}

/** The rows a stored latch keeps: all of them, or the newest {@link LATCH_MAX_ROWS} in first-seen order. */
function rowsToStore(rows: readonly CanRow[]): CanRow[] {
  let kept = rows;
  if (rows.length > LATCH_MAX_ROWS) {
    const newest = new Set([...rows].sort((a, b) => b.timeS - a.timeS).slice(0, LATCH_MAX_ROWS));
    kept = rows.filter((row) => newest.has(row));
  }
  // A restored row has no previous value, so it has nothing to flash.
  return kept.map((row) => {
    const copy = { ...row };
    delete copy.flash;
    return copy;
  });
}

/** Write one latch to storage. A full or refused store keeps the panel working, without the restore. */
function persistLatch(
  panelKey: string,
  state: CanPanelState,
  storage: Storage | null = sessionStore(),
): void {
  if (!storage) return;
  const stored: StoredLatch = {
    version: 1,
    rows: rowsToStore(state.snapshot),
    scope: state.scope,
    deepReadDone: state.deepReadDone,
  };
  try {
    storage.setItem(latchKey(panelKey), JSON.stringify(stored));
  } catch {
    // Quota or a blocked store: the in-memory latch is still right for this document.
  }
}

/** Advance one instance's latch by one poll, and persist it whenever the rows moved. */
export function advanceLatch(
  panelKey: string,
  inputs: CanScopeInputs,
  storage: Storage | null = sessionStore(),
): CanRow[] {
  const state = panelLatch(panelKey, storage);
  const before = state.snapshot;
  const rows = advanceCanScope(state, inputs);
  if (rows !== before) persistLatch(panelKey, state, storage);
  return rows;
}

/** The deep first window has landed; a reload of this instance polls shallowly from here on. */
export function markDeepReadDone(panelKey: string, storage: Storage | null = sessionStore()): void {
  const state = panelLatch(panelKey, storage);
  if (state.deepReadDone) return;
  state.deepReadDone = true;
  persistLatch(panelKey, state, storage);
}

/** Drop every latch this document holds in memory; storage keeps them. A reload does the same. */
export function forgetLatches(): void {
  LATCHES.clear();
}
