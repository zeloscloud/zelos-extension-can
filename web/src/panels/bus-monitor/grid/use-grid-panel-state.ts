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
import {
  type ChangeEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useInsertionEffect,
  useMemo,
  useRef,
  useState,
} from "react";
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

/** Text filter and natural sort on every column, declared once so no column can differ. */
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

/** Search text and column layout: session state, so a reload keeps them. */
interface GridSessionState {
  quickFilterText: string;
  columnState: ColumnState[] | null;
}

const EMPTY_SESSION: GridSessionState = { quickFilterText: "", columnState: null };

function sessionKey(panelKey: string): string {
  return `zelos.panel.${panelKey}.grid`;
}

function readSession(panelKey: string): GridSessionState {
  try {
    const raw = globalThis.sessionStorage?.getItem(sessionKey(panelKey));
    const stored = raw ? (JSON.parse(raw) as Partial<GridSessionState>) : null;
    return {
      quickFilterText: typeof stored?.quickFilterText === "string" ? stored.quickFilterText : "",
      columnState: Array.isArray(stored?.columnState) ? stored.columnState : null,
    };
  } catch {
    return EMPTY_SESSION;
  }
}

function writeSession(panelKey: string, state: GridSessionState): void {
  try {
    globalThis.sessionStorage?.setItem(sessionKey(panelKey), JSON.stringify(state));
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
  return (
    a.length === b.length && a.every((state, i) => b[i] !== undefined && sameState(state, b[i]))
  );
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The saved `options.grid.filterModel`, when it is one: `setFilterModel` throws on a malformed one. */
function savedFilterModel(options: Record<string, unknown> | null): FilterModel | undefined {
  const grid = options?.grid;
  if (!isPlainObject(grid)) return undefined;
  const model = grid.filterModel;
  return isPlainObject(model) && Object.values(model).every(isPlainObject)
    ? (model as FilterModel)
    : undefined;
}

/** A right-clicked row id against the latest rows, scanned on demand: the lookup fires only on right-click. */
export function useGridRowLookup<TRow extends { id: string }>(
  rows: readonly TRow[],
): (rowId: string) => TRow | null {
  const rowsRef = useRef(rows);
  useInsertionEffect(() => {
    rowsRef.current = rows;
  }, [rows]);
  return useCallback(
    (rowId: string) => rowsRef.current.find((row) => row.id === rowId) ?? null,
    [],
  );
}

const NO_OPTION_DEFAULTS = {};

/** The grid minus its rows. Layout and search are session state; the filter model is saved with the layout. */
export function useGridPanelState<TRow extends { id: string }>(
  panel: AppBridgePanelInfo,
  stateKey: string,
  overlays: { noRows: () => ReactNode; loading: () => ReactNode },
) {
  const theme = useTheme();
  const [, setOptions] = usePanelOptions(NO_OPTION_DEFAULTS);
  const [session, setSession] = useState(() => readSession(stateKey));
  const gridApiRef = useRef<GridApi<TRow> | null>(null);

  useEffect(() => writeSession(stateKey, session), [stateKey, session]);

  const getGridApi = useCallback(() => {
    const api = gridApiRef.current;
    return api && !api.isDestroyed() ? api : null;
  }, []);

  const getRowId = useCallback((params: GetRowIdParams<TRow>) => params.data.id, []);

  // Font size drives the THEME, not a per-cell style: AG Grid sizes its own chrome from the theme.
  const fontSize = resolveFontSizePx(panel.options?.fontSize);
  const gridTheme = useMemo(() => buildGridTheme(theme, fontSize), [theme, fontSize]);
  const rowHeight = gridRowHeight(fontSize);

  // Read once at mount: every edit writes the options, so re-reading them would fight the edit.
  const [initialFilterModel] = useState(() => savedFilterModel(panel.options));

  // Read live: the grid can remount inside a living panel, and a mount-time copy would drop every change since.
  const columnStateRef = useRef(session.columnState);
  useInsertionEffect(() => {
    columnStateRef.current = session.columnState;
  }, [session.columnState]);

  const onGridReady = useCallback(
    (event: GridReadyEvent<TRow>) => {
      gridApiRef.current = event.api;
      if (initialFilterModel) event.api.setFilterModel(initialFilterModel);
      const columnState = columnStateRef.current;
      if (columnState) event.api.applyColumnState({ state: columnState, applyOrder: true });
    },
    [initialFilterModel],
  );

  // Debounced, and flushed on unmount and on unload, which runs no React cleanup, so the last click still lands.
  const pendingFilter = useRef<{ model: FilterModel; timer: ReturnType<typeof setTimeout> } | null>(
    null,
  );
  const flushFilter = useCallback(() => {
    const pending = pendingFilter.current;
    if (!pending) return;
    clearTimeout(pending.timer);
    pendingFilter.current = null;
    setOptions({ grid: { filterModel: pending.model } }).catch(() => {});
  }, [setOptions]);
  useEffect(() => {
    window.addEventListener("pagehide", flushFilter);
    return () => {
      window.removeEventListener("pagehide", flushFilter);
      flushFilter();
    };
  }, [flushFilter]);

  // AG Grid re-fires `filterChanged` for the model applied at mount; `{}` is an unfiltered grid.
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
    const next = api.getColumnState().map((state) => {
      const copy = { ...state };
      delete copy.hide;
      return copy;
    });
    setSession((prev) =>
      prev.columnState && columnStatesEqual(prev.columnState, next)
        ? prev
        : { ...prev, columnState: next },
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
    if (compiled.current?.pattern !== pattern)
      compiled.current = { pattern, matcher: createSearchMatcher(pattern) };
    // AG Grid joins a row's columns with newlines; the reader sees spaces.
    return compiled.current.matcher(rowText.replace(/\n/g, " "));
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
