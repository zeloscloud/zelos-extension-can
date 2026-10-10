import { advanceCanScope, type CanPanelState, type CanRow, type CanRowScope, type CanScopeInputs } from "./data";

/**
 * The latch outlives the panel's document. The host reloads the panel on a layout tab switch, so a latch
 * held only in memory would empty every time the user looked away; it is kept in sessionStorage per panel
 * instance and restored on load. Session-only on purpose: a new session starts from the data.
 *
 * Restoring keeps the scope the rows were latched under, so the same rule that clears a live latch clears a
 * restored one: a bound path that left, another workspace, or a cursor that moved back.
 */

/** The most rows a stored latch keeps: the newest by time. A bus has far fewer ids than this. */
export const LATCH_MAX_ROWS = 5_000;

export function latchKey(instanceId: string): string {
  return `zelos.panel.${instanceId}.latch`;
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

function restore(instanceId: string, storage: Storage | null): CanPanelState {
  const state = emptyLatch();
  let stored: Partial<StoredLatch> | null = null;
  try {
    const raw = storage?.getItem(latchKey(instanceId));
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
export function panelLatch(instanceId: string, storage: Storage | null = sessionStore()): CanPanelState {
  let state = LATCHES.get(instanceId);
  if (!state) {
    state = restore(instanceId, storage);
    LATCHES.set(instanceId, state);
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
  return kept.map(({ flash: _flash, ...row }) => row);
}

/** Write one latch to storage. A full or refused store keeps the panel working, without the restore. */
function persistLatch(instanceId: string, state: CanPanelState, storage: Storage | null = sessionStore()): void {
  if (!storage) return;
  const stored: StoredLatch = {
    version: 1,
    rows: rowsToStore(state.snapshot),
    scope: state.scope,
    deepReadDone: state.deepReadDone,
  };
  try {
    storage.setItem(latchKey(instanceId), JSON.stringify(stored));
  } catch {
    // Quota or a blocked store: the in-memory latch is still right for this document.
  }
}

/** Advance one instance's latch by one poll, and persist it whenever the rows moved. */
export function advanceLatch(
  instanceId: string,
  inputs: CanScopeInputs,
  storage: Storage | null = sessionStore(),
): CanRow[] {
  const state = panelLatch(instanceId, storage);
  const before = state.snapshot;
  const rows = advanceCanScope(state, inputs);
  if (rows !== before) persistLatch(instanceId, state, storage);
  return rows;
}

/** The deep first window has landed; a reload of this instance polls shallowly from here on. */
export function markDeepReadDone(instanceId: string, storage: Storage | null = sessionStore()): void {
  const state = panelLatch(instanceId, storage);
  if (state.deepReadDone) return;
  state.deepReadDone = true;
  persistLatch(instanceId, state, storage);
}

/** Drop every latch this document holds in memory; storage keeps them. A reload does the same. */
export function forgetLatches(): void {
  LATCHES.clear();
}
