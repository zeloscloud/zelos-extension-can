import type { ColDef, ICellRendererParams, SortDirection } from "ag-grid-community";
import { memo } from "react";
import { CAN_EVENT_LINE_CLASS, type CanRow, canDataText, compareCanMessage } from "./data";
import { GridCell, GridTextCell, MONO_VALUE_CELL, text } from "./grid/chrome";
import { formatTime } from "./time";

/**
 * The columns are a fixed schema over a LATCHED row — Timestamp, Message, DLC, Data — not one column per
 * signal, so there is no per-column display format or removal. The table a row came from is the Message
 * cell's hover text, and a Source column only when the panel asks for it: every row of a bus repeats the
 * same path.
 *
 * Sorting is offered on the two columns that identify a row (Message, Timestamp) and withheld from the
 * rest: a grid sorted by DLC, a byte string, or a path every row shares tells you nothing and shuffles a
 * bus you are reading by id.
 */

/** What the DLC column shows for a row that has none. */
const NO_DLC = "-";

/** asc → desc, never AG Grid's third "no sort" state, which would drop the decoded/raw split. */
const MESSAGE_SORT_ORDER: SortDirection[] = ["asc", "desc"];

/** A row's time in the mode its own feed resolved its window in: the feeds resolve their windows separately,
 *  so for a moment after a mode switch two rows on screen can differ. */
const timeText = (row: CanRow | undefined) => (row ? formatTime(row.timeS, row.timeMode) : "");

/**
 * The event name or the frame's hex id, with the full table path on hover. A frame id is a fixed-width
 * number and is centered like the DLC beside it; an event name is text and stays left.
 *
 * Centering is a class on the renderer's OWN flex row, not on the cell: AG Grid puts an
 * `.ag-cell-wrapper` and a `flex: 1 1 auto` `.ag-cell-value` span between `.ag-cell` and the renderer, so
 * a justify rule on the cell lands on the wrapper, whose one item already fills it, and never moves text.
 */
const CanMessageCell = memo((props: ICellRendererParams<CanRow>) => {
  const { data } = props;
  if (!data) return null;
  return (
    <GridCell className={data.kind === "frame" ? "truncate centered" : "truncate"} title={data.source}>
      {data.message}
    </GridCell>
  );
});
CanMessageCell.displayName = "CanMessageCell";

/** The DLC, centered under its header — see {@link CanMessageCell} for where the alignment lives. */
const CanDlcCell = memo((props: ICellRendererParams<CanRow>) => {
  const { data, value } = props;
  if (!data) return null;
  return <GridCell className="truncate centered">{text(value)}</GridCell>;
});
CanDlcCell.displayName = "CanDlcCell";

/** Per line, so a long decoded value is cut at the column edge instead of widening the row. */
const CanDataCell = memo((props: ICellRendererParams<CanRow> & { flash: boolean }) => {
  const { data, flash } = props;
  if (!data) return null;

  const flashed = flash ? data.flash : undefined;
  const flashClass = (index: number) => (flashed?.includes(index) ? "can-flash" : undefined);

  // The token is part of the key on purpose: a changed one remounts, which is what replays the flash
  // animation, while an unchanged one keeps its node and stays still.
  if (data.kind === "frame") {
    return (
      <GridCell className="can-bytes">
        {data.tokens.map((byte, index) => (
          <span key={`${index}:${byte}`} className={flashClass(index)}>
            {byte}
          </span>
        ))}
      </GridCell>
    );
  }

  // One `title` on the cell, not one per line: a decoded message is a handful of lines and the row height
  // is already sized for all of them.
  return (
    <GridCell className={CAN_EVENT_LINE_CLASS} title={canDataText(data)}>
      {data.tokens.map((line, index) => {
        const lineFlash = flashClass(index);
        return (
          <div key={`${index}:${line}`} className={lineFlash ? `truncate ${lineFlash}` : "truncate"} data-can-line="">
            {line}
          </div>
        );
      })}
    </GridCell>
  );
});
CanDataCell.displayName = "CanDataCell";

interface CanColumnDefsParams {
  /** The panel's `Flash changes` option; off, the cells never carry the class at all. */
  flashChanges: boolean;
  /** The panel's `Show Source` option. */
  showSource: boolean;
}

export function buildCanColumnDefs({ flashChanges, showSource }: CanColumnDefsParams): ColDef<CanRow>[] {
  return [
    {
      // First and pinned: when a row was last seen is what you read a latched grid against. Everything else
      // is what that row IS. No initial sort: Message's is the only one, and a two-column sort would number
      // the headers (`1`, `2`).
      colId: "timestamp",
      headerName: "Timestamp",
      pinned: "left",
      // Sized for the whole instant: absolute mode renders 32 characters
      // (`YYYY-MM-DDTHH:MM:SS.uuuuuu±HH:MM`) at about 8.4px each in a 14px mono, plus the cell's 32px of
      // horizontal padding.
      width: 310,
      cellClass: MONO_VALUE_CELL,
      // Sorted by the instant; filter and search match the rendered time, so you filter what you can read.
      comparator: (_a, _b, nodeA, nodeB) => (nodeA.data?.timeS ?? 0) - (nodeB.data?.timeS ?? 0),
      valueGetter: (p) => p.data?.timeS,
      valueFormatter: (p) => timeText(p.data),
      filterValueGetter: (p) => timeText(p.data),
      getQuickFilterText: (p) => timeText(p.data),
      cellRenderer: GridTextCell,
    },
    {
      colId: "message",
      headerName: "Message",
      width: 210,
      initialSort: "asc",
      sortingOrder: MESSAGE_SORT_ORDER,
      comparator: (_a, _b, nodeA, nodeB, descending) => compareCanMessage(nodeA.data, nodeB.data, descending),
      cellClass: MONO_VALUE_CELL,
      valueGetter: (p) => p.data?.message ?? "",
      cellRenderer: CanMessageCell,
    },
    {
      colId: "dlc",
      headerName: "DLC",
      // Room for the label beside the header's filter button, so it never reads "D…".
      width: 84,
      minWidth: 84,
      sortable: false,
      cellClass: MONO_VALUE_CELL,
      // An event has no DLC, and the dash says so rather than borrowing its field count.
      valueGetter: (p) => (p.data?.dlc == null ? NO_DLC : text(p.data.dlc)),
      cellRenderer: CanDlcCell,
    },
    {
      colId: "data",
      headerName: "Data",
      flex: 1,
      minWidth: 240,
      sortable: false,
      cellClass: MONO_VALUE_CELL,
      valueGetter: (p) => (p.data ? canDataText(p.data) : ""),
      cellRenderer: CanDataCell,
      cellRendererParams: { flash: flashChanges },
    },
    {
      colId: "source",
      headerName: "Source",
      width: 160,
      hide: !showSource,
      sortable: false,
      cellClass: MONO_VALUE_CELL,
      valueGetter: (p) => p.data?.source ?? "",
      cellRenderer: GridTextCell,
    },
  ];
}
