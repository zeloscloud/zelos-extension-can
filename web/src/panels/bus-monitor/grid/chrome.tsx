import type { ICellRendererParams } from "ag-grid-community";
import {
  type ChangeEvent,
  createContext,
  type KeyboardEvent,
  memo,
  type MouseEvent,
  type ReactNode,
  useCallback,
  useContext,
  useMemo,
  useRef,
} from "react";
import { AlertIcon, ClearIcon, SearchIcon } from "../icons";
import { escapeRegExp, extractHighlightTerms } from "./search";

/** `cellClass` for a value column: monospace with tabular figures, so numbers, timestamps and hex line up. */
export const MONO_VALUE_CELL = "mono-value";

/** A cell value as text: null/absent renders as `""`, not `"null"`. */
export function text(value: unknown): string {
  return value == null ? "" : String(value);
}

/** A centered icon and one line: the panel's empty and loading states. */
export function PanelState({ icon, description }: { icon?: ReactNode; description: string }) {
  return (
    <div className="panel-state" role="status">
      {icon}
      <p>{description}</p>
    </div>
  );
}

/** A failed read: the alert icon, a heading, and the cause in small type. */
export function PanelErrorState({ message }: { message: string }) {
  return (
    <div className="panel-state error" role="alert">
      <AlertIcon />
      <p>Error loading data</p>
      <p className="panel-state-detail">{message}</p>
    </div>
  );
}

/**
 * Search terms for cell highlighting. Context rather than column defs: republishing the column defs on every
 * keystroke would rebuild the whole column pipeline, while a context update only repaints custom cells.
 */
const GridSearchContext = createContext<string[]>([]);

/** `text` with every search term marked. */
const Highlight = memo(({ value, terms }: { value: string; terms: string[] }) => {
  if (terms.length === 0) return <>{value}</>;
  const pattern = new RegExp(`(${terms.map(escapeRegExp).join("|")})`, "gi");
  const lower = terms.map((term) => term.toLowerCase());
  return (
    <>
      {value.split(pattern).map((part, index) =>
        lower.includes(part.toLowerCase()) ? (
          <mark key={index} className="search-hit">
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </>
  );
});
Highlight.displayName = "Highlight";

/**
 * A grid cell's content, with search terms highlighted. Non-string children render as-is.
 *
 * role/tabIndex compensate for `suppressCellFocus`, so keyboard users can still reach the cell's context
 * menu. Not a real `<button>`: that would fight `enableCellTextSelection`.
 */
export function GridCell({
  className,
  title,
  children,
}: {
  className?: string | undefined;
  /** Hover text for a cell whose content is clipped; one per CELL, never one per line. */
  title?: string | undefined;
  children: ReactNode;
}) {
  const terms = useContext(GridSearchContext);
  return (
    <div
      className={className ? `grid-cell ${className}` : "grid-cell"}
      title={title}
      tabIndex={0}
      role="button"
      aria-haspopup="menu"
    >
      {typeof children === "string" ? (
        // Its own box, so a clipped value ends in an ellipsis rather than mid-glyph.
        <span className="cell-text">
          <Highlight value={children} terms={terms} />
        </span>
      ) : (
        children
      )}
    </div>
  );
}

/** A column whose cell is just its text. */
export const GridTextCell = memo((props: ICellRendererParams) => {
  const { data, value, valueFormatted } = props;
  if (!data) return null;
  return <GridCell className="truncate">{valueFormatted ?? text(value)}</GridCell>;
});
GridTextCell.displayName = "GridTextCell";

/** The cell a right-click landed on; `text` is what it showed on screen at that moment. */
export interface GridMenuCell {
  readonly colId: string;
  readonly rowId: string;
  readonly text: string;
}

/** A right-click on a cell, with where it happened in the panel document. */
export interface GridMenuRequest {
  readonly cell: GridMenuCell;
  readonly x: number;
  readonly y: number;
}

/**
 * The cell a context-menu event landed in, read from the DOM at that moment. AG Grid recycles row
 * elements, so the menu works from this snapshot and never from the cell's own React subtree.
 */
function cellAt(event: MouseEvent): GridMenuCell | null {
  const element = event.target instanceof Element ? event.target : null;
  const cell = element?.closest(".ag-cell");
  const colId = cell?.getAttribute("col-id");
  const rowId = cell?.closest(".ag-row")?.getAttribute("row-id");
  if (!cell || !colId || !rowId) return null;
  return { colId, rowId, text: cell.textContent?.trim() ?? "" };
}

/**
 * A grid panel's frame: error state, search box, the body the grid fills, and one status line under it.
 * The body's `min-height: 0` is what lets the grid shrink with the panel.
 */
export function GridPanelBody({
  error,
  search,
  footer,
  onCellMenu,
  children,
}: {
  error: string | null;
  search: { value: string; onChange: (event: ChangeEvent<HTMLInputElement>) => void; onClear: () => void };
  /** One line under the grid; a panel with nothing to say passes nothing. */
  footer?: ReactNode | undefined;
  /** A right-click on a cell. A right-click anywhere else keeps the document's own menu out of the way. */
  onCellMenu: (request: GridMenuRequest) => void;
  children: ReactNode;
}) {
  const searchRef = useRef<HTMLInputElement>(null);

  // Cmd+F focuses this panel's search while focus is inside it.
  const handleKeyDown = useCallback((event: KeyboardEvent) => {
    if ((event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey && event.key.toLowerCase() === "f") {
      event.preventDefault();
      event.stopPropagation();
      searchRef.current?.focus();
    }
  }, []);

  const handleContextMenu = useCallback(
    (event: MouseEvent) => {
      event.preventDefault();
      const cell = cellAt(event);
      if (cell) onCellMenu({ cell, x: event.clientX, y: event.clientY });
    },
    [onCellMenu],
  );

  const terms = useMemo(() => extractHighlightTerms(search.value), [search.value]);

  if (error) return <PanelErrorState message={error} />;

  return (
    // tabIndex makes any click in the panel count as focus, so Cmd+F reaches the search after a click.
    <div className="grid-panel" tabIndex={-1} onKeyDown={handleKeyDown} data-grid-panel="">
      <div className="grid-search">
        <label className="grid-search-box">
          <SearchIcon />
          <input
            ref={searchRef}
            type="text"
            placeholder="Search..."
            aria-label="Search"
            value={search.value}
            onChange={search.onChange}
          />
          {search.value && (
            <button type="button" aria-label="Clear search" title="Clear search" onClick={search.onClear}>
              <ClearIcon />
            </button>
          )}
        </label>
      </div>
      <div className="grid-body" onContextMenu={handleContextMenu}>
        <GridSearchContext.Provider value={terms}>{children}</GridSearchContext.Provider>
      </div>
      {footer}
    </div>
  );
}

/**
 * The status line under the grid when the window held more rows than the panel asked for: what is on screen
 * is its newest slice and a row outside it is missing. Said in the panel's own chrome rather than inferred
 * from a row that never appears. Nothing to say renders nothing.
 */
export function WindowStatus({ shown, total }: { shown: number; total: number }) {
  if (total <= shown) return null;
  return (
    <div className="grid-status" data-testid="can-window-status">
      {`window: newest ${shown.toLocaleString()} of ${total.toLocaleString()} rows`}
    </div>
  );
}
