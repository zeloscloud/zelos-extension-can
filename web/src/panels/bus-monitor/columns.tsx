import type { ColDef, ICellRendererParams, SortDirection } from "ag-grid-community";
import { memo } from "react";
import { GRID_CELL_HORIZONTAL_PADDING_PX, HEADER_FONT_FAMILY } from "./ag-grid-theme";
import { CAN_EVENT_LINE_CLASS, type CanRow, canDataText, compareCanMessage } from "./data";
import { GridCell, GridTextCell, MONO_VALUE_CELL, text } from "./grid/chrome";
import { formatTime, type TimeMode } from "./time";

let measureContext: CanvasRenderingContext2D | null | undefined;

/** Quartz's header filter button, its 8 px spacing from the label, and the sort indicator. */
const HEADER_FILTER_BUTTON_PX = 16 + 8;
const HEADER_SORT_INDICATOR_PX = 24;

/** The Data cell's bytes sit this far apart (`.can-bytes`). */
const BYTE_GAP_PX = 6;

/** The measured widths at the panel's font: floors, and where the Message column starts. */
export interface CanColumnWidths {
  timestamp: number;
  message: number;
  dlc: number;
  data: number;
}

/** The Message column starts with room for a name this many characters long. */
const MESSAGE_START_CHARS = 21;

/**
 * Each floor holds its header (label, filter button, sort indicator) and its widest content in the
 * cells' mono face: the whole time (an elapsed one is widest at an end), a DLC, a classic frame's 8 bytes.
 * Message has no floor; it starts with room for its header and a {@link MESSAGE_START_CHARS}-character name.
 */
export function canColumnWidths(
  mode: TimeMode,
  fontSizePx: number,
  startS: number,
  endS: number,
): CanColumnWidths | undefined {
  measureContext ??= document.createElement("canvas").getContext("2d");
  const measure = measureContext;
  if (!measure) return undefined;
  const width = (font: string, value: string) => {
    measure.font = font;
    return measure.measureText(value).width;
  };
  const padded = (px: number) => Math.ceil(px) + 2 * GRID_CELL_HORIZONTAL_PADDING_PX;
  const header = (label: string, sortable: boolean) =>
    padded(
      width(`500 ${fontSizePx}px ${HEADER_FONT_FAMILY}`, label) +
        HEADER_FILTER_BUTTON_PX +
        (sortable ? HEADER_SORT_INDICATOR_PX : 0),
    );
  const mono = getComputedStyle(document.documentElement).getPropertyValue("--mono").trim() || "monospace";
  const cell = (value: string) => width(`${fontSizePx}px ${mono}`, value);
  const times = mode === "relative" ? [startS, endS] : [0];
  const widestTime = Math.max(...times.map((timeS) => cell(formatTime(timeS, mode))));
  return {
    timestamp: Math.max(header("Timestamp", true), padded(widestTime)),
    message: Math.max(header("Message", true), padded(cell("0".repeat(MESSAGE_START_CHARS)))),
    dlc: Math.max(header("DLC", false), padded(cell("64"))),
    data: Math.max(header("Data", false), padded(8 * cell("FF") + 7 * BYTE_GAP_PX)),
  };
}

/** What the DLC column shows for a row that has none. */
const NO_DLC = "-";

/** asc → desc, never AG Grid's third "no sort" state, which would drop the decoded/raw split. */
const MESSAGE_SORT_ORDER: SortDirection[] = ["asc", "desc"];

/** A row's time in the mode its own feed resolved its window in. */
const timeText = (row: CanRow | undefined) => (row ? formatTime(row.timeS, row.timeMode) : "");

/**
 * The event name, left as text; a frame's id is a number, centered like the DLC beside it. The class
 * sits on the renderer's own row: a rule on `.ag-cell` never moves text.
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

  // The token is part of the key: a changed one remounts, which replays the flash.
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

  // One `title` on the cell, not one per line: the row is already tall enough for every line.
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
  /** The measured column floors; undefined where nothing can measure text. */
  widths: CanColumnWidths | undefined;
}

/** Only the columns that identify a row sort: a bus sorted by its bytes shuffles the ids you read it by. */
export function buildCanColumnDefs({ flashChanges, showSource, widths }: CanColumnDefsParams): ColDef<CanRow>[] {
  return [
    {
      // No initial sort: a second one beside Message's would number the headers (`1`, `2`).
      colId: "timestamp",
      headerName: "Timestamp",
      pinned: "left",
      // A floor, not only a start, so a larger font or an older saved width never cuts the time.
      ...(widths && { initialWidth: widths.timestamp, minWidth: widths.timestamp }),
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
      ...(widths && { initialWidth: widths.message }),
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
      ...(widths && { initialWidth: widths.dlc, minWidth: widths.dlc }),
      sortable: false,
      cellClass: MONO_VALUE_CELL,
      // An event has no DLC, and the dash says so rather than borrowing its field count.
      valueGetter: (p) => (p.data?.dlc == null ? NO_DLC : text(p.data.dlc)),
      cellRenderer: CanDlcCell,
    },
    {
      colId: "data",
      headerName: "Data",
      initialFlex: 1,
      ...(widths && { minWidth: widths.data }),
      sortable: false,
      cellClass: MONO_VALUE_CELL,
      valueGetter: (p) => (p.data ? canDataText(p.data) : ""),
      cellRenderer: CanDataCell,
      cellRendererParams: { flash: flashChanges },
    },
    {
      colId: "source",
      headerName: "Source",
      initialWidth: 160,
      hide: !showSource,
      sortable: false,
      cellClass: MONO_VALUE_CELL,
      valueGetter: (p) => p.data?.source ?? "",
      cellRenderer: GridTextCell,
    },
  ];
}
