import type { PanelMenuItem } from "@zeloscloud/app-extension-sdk";
import { type CanRow, canDataText } from "./data";

/** What a right-click on a cell acts on: the latched row it landed in, and the cell it landed on. */
interface CanMenuTarget {
  readonly row: CanRow;
  /** What "Copy value" copies — see {@link canCellText}. */
  readonly cellText: string;
}

/**
 * The cell's text as a reader would write it down. The DOM is not it for the Data cell: its tokens are
 * adjacent elements, so `textContent` runs them together (`00010203`, `a = 7b = 8`). Every other column
 * renders one string, and the on-screen text is what a right-click should copy.
 */
export function canCellText(row: CanRow, colId: string, domText: string): string {
  return colId === "data" ? canDataText(row) : domText;
}

/**
 * The row as a machine reads it: what is on screen. A row is already a reduction (the newest frame for an
 * id, a decoded message's fields as lines), so the display IS the record.
 */
function canRowJson(row: CanRow): Record<string, unknown> {
  return { source: row.source, message: row.message, dlc: row.dlc, time_s: row.timeS, data: [...row.tokens] };
}

/**
 * A cell's context menu: copy value → copy bytes → copy row. Cell-scoped, with no display format and no
 * remove — a column is a fixed field, not a signal.
 *
 * No "Set cursor here": the grid is a latch, not a sequence, so its rows are not a place in time to go to.
 */
export function canMenuItems(target: CanMenuTarget): PanelMenuItem[] {
  const items: PanelMenuItem[] = [];
  // Hidden on an empty cell: there is nothing to copy.
  if (target.cellText) items.push({ id: "copy-value", label: "Copy value", icon: "copy" });
  // Absent, not disabled, on a decoded row: its Data cell is signal lines, which "Copy value" copies.
  if (target.row.kind === "frame") items.push({ id: "copy-bytes", label: "Copy bytes", icon: "bytes" });
  // The cell's copies, then the row's: a separator between the two.
  const last = items.at(-1);
  if (last) last.separatorAfter = true;
  items.push({ id: "copy-row-json", label: "Copy row as JSON", icon: "json" });
  return items;
}

/** The text a chosen item copies and the toast that confirms it, or null for no choice. */
export function canMenuCopy(itemId: string | null, target: CanMenuTarget): { text: string; toast: string } | null {
  switch (itemId) {
    case "copy-value":
      return { text: target.cellText, toast: "Value copied" };
    case "copy-bytes":
      return target.row.kind === "frame" ? { text: canDataText(target.row), toast: "Bytes copied" } : null;
    case "copy-row-json":
      return { text: JSON.stringify(canRowJson(target.row)), toast: "Row copied as JSON" };
    default:
      return null;
  }
}
