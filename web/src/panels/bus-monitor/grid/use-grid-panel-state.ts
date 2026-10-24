import type { AppBridgePanelInfo } from "@zeloscloud/app-extension-sdk";
import { usePanelOptions, useTheme } from "@zeloscloud/app-extension-sdk/react";
import {
  AllCommunityModule,
  type ColDef,
  type ColumnMovedEvent,
  type ColumnResizedEvent,
  type ColumnState,
  type FilterModel,
  type GetRowIdParams,
  type GridApi,
  type GridReadyEvent,
  ModuleRegistry,
} from "ag-grid-community";
import { type ChangeEvent, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { buildGridTheme } from "../ag-grid-theme";
import { compareNaturalMixed } from "../natural-compare";
import { gridRowHeight, resolveFontSizePx } from "../options";
import { createSearchMatcher } from "./search";

ModuleRegistry.registerModules([AllCommunityModule]);

const GRID_PANEL_PROPS = {
  animateRows: false,
  suppressAnimationFrame: true,
  suppressCellFocus: true,
  enableCellTextSelection: true,
  cacheQuickFilter: true,
  // Community has no columns tool panel, so a column dragged off the grid couldn't be brought back.
  suppressDragLeaveHidesColumns: true,
  // Keep the user's column order when the column defs change.
  maintainColumnOrder: true,
  // AG Grid closes an open filter popup on every body scroll; a polled grid scrolls often.
  suppressScrollWhenPopupsAreOpen: true,
} as const;

const MIN_HEADER_HEIGHT = 32;

/** A filter popup fires one `filterChanged` per checkbox; each write reaches the saved layout. */
const FILTER_PERSIST_DEBOUNCE_MS = 250;

/** Text filter and natural sort on every column, declared once so no column can differ. Columns filter on
 *  the text they SHOW (via `filterValueGetter`), so you filter by what you can read. */
const GRID_DEFAULT_COL_DEF: ColDef = {
  resizable: true,
  sortable: true,
  comparator: compareNaturalMixed,
  filter: "agTextColumnFilter",
};

/** The whole quick-filter text is one glob, matched by {@link createSearchMatcher}. */
const gridQuickFilterParser = (quickFilter: string): string[] => {
  const pattern = quickFilter.trim();
  return pattern ? [pattern] : [];
};

/** Search text and column layout: session state, kept per panel instance so a reload keeps them. */
interface GridSessionState {
  quickFilterText: string;
  columnState: ColumnState[] | null;
}

const EMPTY_SESSION: GridSessionState = { quickFilterText: "", columnState: null };

function sessionKey(instanceId: string): string {
  return `zelos.panel.${instanceId}.grid`;
}

function readSession(instanceId: string): GridSessionState {
  try {
    const raw = globalThis.sessionStorage?.getItem(sessionKey(instanceId));
    const stored = raw ? (JSON.parse(raw) as Partial<GridSessionState>) : null;
    return {
      quickFilterText: typeof stored?.quickFilterText === "string" ? stored.quickFilterText : "",
      columnState: Array.isArray(stored?.columnState) ? stored.columnState : null,
    };
  } catch {
    return EMPTY_SESSION;
  }
}

function writeSession(instanceId: string, state: GridSessionState): void {
  try {
    globalThis.sessionStorage?.setItem(sessionKey(instanceId), JSON.stringify(state));
  } catch {
    // A refused store only costs the restore after a reload.
  }
}

/** The persisted column fields. `hide` is left out: visibility is a panel option driving `ColDef.hide`. */
function sameState(a: ColumnState, b: ColumnState): boolean {
  return (
    a.colId === b.colId &&
    a.width === b.width &&
    a.flex === b.flex &&
    a.pinned === b.pinned &&
    a.sort === b.sort &&
    a.sortIndex === b.sortIndex
  );
}

function columnStatesEqual(a: ColumnState[], b: ColumnState[]): boolean {
  return a.length === b.length && a.every((state, i) => b[i] !== undefined && sameState(state, b[i]));
}

/** The saved filter model under `options.grid.filterModel`, when it is one. */
function savedFilterModel(options: Record<string, unknown> | null): FilterModel | undefined {
  const grid = options?.grid;
  if (!grid || typeof grid !== "object") return undefined;
  const model = (grid as { filterModel?: unknown }).filterModel;
  return model && typeof model === "object" ? (model as FilterModel) : undefined;
}

/**
 * Resolve a right-clicked row id against the latest rows. Scans on demand rather than indexing: the rows
 * replace wholesale every poll, and the lookup fires only on right-click. The returned function is stable.
 */
export function useGridRowLookup<TRow extends { id: string }>(rows: readonly TRow[]): (rowId: string) => TRow | null {
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  return useCallback((rowId: string) => rowsRef.current.find((row) => row.id === rowId) ?? null, []);
}

const NO_OPTION_DEFAULTS = {};

/**
 * Everything the grid is, minus its rows: the api handle, theme and font, row height, search, and the
 * filter and column-state round-trips. Column layout and search text are session state; the filter model
 * is panel state, saved with the layout.
 */
export function useGridPanelState<TRow extends { id: string }>(
  panel: AppBridgePanelInfo,
  overlays: { noRows: () => ReactNode; loading: () => ReactNode },
) {
  const instanceId = panel.instanceId;
  const theme = useTheme();
  const [, setOptions] = usePanelOptions(NO_OPTION_DEFAULTS);
  const [session, setSession] = useState(() => readSession(instanceId));
  const gridApiRef = useRef<GridApi<TRow> | null>(null);

  useEffect(() => writeSession(instanceId, session), [instanceId, session]);

  const getGridApi = useCallback(() => {
    const api = gridApiRef.current;
    return api && !api.isDestroyed() ? api : null;
  }, []);

  const getRowId = useCallback((params: GetRowIdParams<TRow>) => params.data.id, []);

  // Font size drives the THEME, not a per-cell style: AG Grid sizes its own chrome from the theme.
  const fontSize = resolveFontSizePx(panel.options?.fontSize);
  const gridTheme = useMemo(() => buildGridTheme(theme, fontSize), [theme, fontSize]);
  const rowHeight = gridRowHeight(fontSize);

  // Snapshotted at MOUNT: re-applying the saved model whenever options change would fight the filter the
  // user is editing right now — every edit writes the options it would read back.
  const [initialFilterModel] = useState(() => savedFilterModel(panel.options));

  // Read LIVE: the grid can remount inside a living panel, and replaying a mount-time snapshot would drop
  // every width and sort set since.
  const columnStateRef = useRef(session.columnState);
  columnStateRef.current = session.columnState;

  const onGridReady = useCallback(
    (event: GridReadyEvent<TRow>) => {
      gridApiRef.current = event.api;
      if (initialFilterModel) event.api.setFilterModel(initialFilterModel);
      const columnState = columnStateRef.current;
      if (columnState) event.api.applyColumnState({ state: columnState, applyOrder: true });
    },
    [initialFilterModel],
  );

  // Debounced, and flushed on unmount so the last click inside the window still lands.
  const pendingFilter = useRef<{ model: FilterModel; timer: ReturnType<typeof setTimeout> } | null>(null);
  const flushFilter = useCallback(() => {
    const pending = pendingFilter.current;
    if (!pending) return;
    clearTimeout(pending.timer);
    pendingFilter.current = null;
    setOptions({ grid: { filterModel: pending.model } }).catch(() => {});
  }, [setOptions]);
  useEffect(() => flushFilter, [flushFilter]);

  // AG Grid re-fires `filterChanged` for the model applied at mount. Compare against the last model SEEN,
  // seeded with `{}` (an unfiltered grid), so a no-op event writes nothing.
  const lastFilterJson = useRef(JSON.stringify(initialFilterModel ?? {}));
  const onFilterChanged = useCallback(() => {
    const api = getGridApi();
    if (!api) return;
    const model = api.getFilterModel();
    const json = JSON.stringify(model);
    if (json === lastFilterJson.current) return;
    lastFilterJson.current = json;
    if (pendingFilter.current) clearTimeout(pendingFilter.current.timer);
    pendingFilter.current = { model, timer: setTimeout(flushFilter, FILTER_PERSIST_DEBOUNCE_MS) };
  }, [getGridApi, flushFilter]);

  const persistColumnState = useCallback(() => {
    const api = getGridApi();
    if (!api) return;
    const next = api.getColumnState().map(({ hide: _hide, ...rest }) => rest);
    setSession((prev) =>
      prev.columnState && columnStatesEqual(prev.columnState, next) ? prev : { ...prev, columnState: next },
    );
  }, [getGridApi]);

  // A resize or move emits an event per mouse move; only the last carries `finished`.
  const onColumnDragFinished = useCallback(
    (event: ColumnResizedEvent<TRow> | ColumnMovedEvent<TRow>) => {
      if (event.finished) persistColumnState();
    },
    [persistColumnState],
  );

  const search = useMemo(
    () => ({
      value: session.quickFilterText,
      onChange: (event: ChangeEvent<HTMLInputElement>) =>
        setSession((prev) => ({ ...prev, quickFilterText: event.target.value })),
      onClear: () => setSession((prev) => ({ ...prev, quickFilterText: "" })),
    }),
    [session.quickFilterText],
  );

  // Compiling a glob isn't free and the matcher runs once per row, so hold the current pattern's matcher.
  const compiled = useRef<{ pattern: string; matcher: (text: string) => boolean } | null>(null);
  const quickFilterMatcher = useCallback((parts: string[], rowText: string) => {
    const pattern = parts[0];
    if (pattern === undefined) return true;
    if (compiled.current?.pattern !== pattern) compiled.current = { pattern, matcher: createSearchMatcher(pattern) };
    return compiled.current.matcher(rowText);
  }, []);

  return {
    search,
    getGridApi,
    gridProps: {
      ...GRID_PANEL_PROPS,
      theme: gridTheme,
      rowHeight,
      // Header text is sized by the same theme font as the rows, so its band grows with them.
      headerHeight: Math.max(MIN_HEADER_HEIGHT, rowHeight),
      defaultColDef: GRID_DEFAULT_COL_DEF,
      noRowsOverlayComponent: overlays.noRows,
      loadingOverlayComponent: overlays.loading,
      getRowId,
      quickFilterText: session.quickFilterText,
      quickFilterParser: gridQuickFilterParser,
      quickFilterMatcher,
      onGridReady,
      onFilterChanged,
      onSortChanged: persistColumnState,
      onColumnResized: onColumnDragFinished,
      onColumnMoved: onColumnDragFinished,
      onColumnVisible: persistColumnState,
      onColumnPinned: persistColumnState,
    },
  };
}
