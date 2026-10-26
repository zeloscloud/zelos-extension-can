import type { PanelMenuItem } from "@zeloscloud/app-extension-sdk";
import { type CanRow, canDataText } from "./data";

/** What a right-click on a cell acts on: the latched row it landed in, and the cell it landed on. */
interface CanMenuTarget {
  readonly row: CanRow;
  /** What "Copy value" copies — see {@link canCellText}. */
  readonly cellText: string;
}

/** The cell's text as a reader writes it: `textContent` runs the Data cell's token elements together. */
export function canCellText(row: CanRow, colId: string, domText: string): string {
  return colId === "data" ? canDataText(row) : domText;
}

/** The row as a machine reads it: a row is already a reduction, so the display is the record. */
function canRowJson(row: CanRow): Record<string, unknown> {
  return { source: row.source, message: row.message, dlc: row.dlc, time_s: row.timeS, data: [...row.tokens] };
}

/** A frame row with bytes to copy; a decoded row's lines are what "Copy value" copies. */
function hasBytes(row: CanRow): boolean {
  return row.kind === "frame" && row.tokens.length > 0;
}

/** A cell's context menu: copy value, copy bytes, copy row. No "Set cursor here": a latch is not a place in time. */
export function canMenuItems(target: CanMenuTarget): PanelMenuItem[] {
  const items: PanelMenuItem[] = [];
  // Hidden on an empty cell: there is nothing to copy.
  if (target.cellText) items.push({ id: "copy-value", label: "Copy value", icon: "copy" });
  if (hasBytes(target.row)) items.push({ id: "copy-bytes", label: "Copy bytes", icon: "bytes" });
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
      return hasBytes(target.row) ? { text: canDataText(target.row), toast: "Bytes copied" } : null;
    case "copy-row-json":
      return { text: JSON.stringify(canRowJson(target.row)), toast: "Row copied as JSON" };
    default:
      return null;
  }
}
