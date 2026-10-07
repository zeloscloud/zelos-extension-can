import type { AppBridgeThemeInfo } from "@zeloscloud/app-extension-sdk";
import { colorSchemeDark, colorSchemeLight, themeQuartz } from "ag-grid-community";

/**
 * The grid's color scheme. The base colors come from the host's theme tokens, so the grid matches the
 * panels around it; the rest mirrors the same neutral/violet palette. Font FACE is not set here — headers
 * stay sans and only value cells opt into mono (`.mono-value`). Font SIZE is a panel option applied on top.
 */

/** A cell's left and right padding. Quartz's own default. */
const GRID_CELL_HORIZONTAL_PADDING_PX = 16;

/** A token's HSL channels as a color, or the fallback before the host has sent any. */
function token(tokens: Record<string, string> | undefined, name: string, fallback: string): string {
  const value = tokens?.[name]?.trim();
  return value ? `hsl(${value})` : fallback;
}

const LIGHT_PARAMS = {
  borderRadius: 0,
  accentColor: "hsl(263.4, 70%, 50.4%)", // violet-700
  headerBackgroundColor: "hsl(0, 0%, 83.1%)", // neutral-300
  headerTextColor: "hsl(0, 0%, 20.9%)", // neutral-800
  headerFontWeight: 500,
  oddRowBackgroundColor: "hsl(0, 0%, 91.5%)",
  rowHoverColor: "hsl(0, 0%, 96.1%)", // neutral-100
  selectedRowBackgroundColor: "hsla(250.5, 95.2%, 91.8%, 0.5)", // violet-200
  rangeSelectionBorderColor: "hsl(263.4, 70%, 50.4%)",
  rangeSelectionBackgroundColor: "hsla(263.4, 70%, 50.4%, 0.1)",
  rangeSelectionHighlightColor: "hsla(263.4, 70%, 50.4%, 0.2)",
  cellHorizontalPadding: GRID_CELL_HORIZONTAL_PADDING_PX,
  rowVerticalPaddingScale: 0.9,
};

const DARK_PARAMS = {
  borderRadius: 0,
  accentColor: "hsl(263.4, 70%, 50.4%)", // violet-700
  headerBackgroundColor: "hsl(0, 0%, 9%)", // neutral-900
  headerTextColor: "hsl(0, 0%, 63.9%)", // neutral-400
  headerFontWeight: 500,
  oddRowBackgroundColor: "hsl(0, 0%, 5.5%)",
  rowHoverColor: "hsl(0, 0%, 9%)", // neutral-900
  selectedRowBackgroundColor: "hsla(263.5, 67.4%, 34.9%, 0.5)", // violet-900
  rangeSelectionBorderColor: "hsl(263.4, 70%, 50.4%)",
  rangeSelectionBackgroundColor: "hsla(263.4, 70%, 50.4%, 0.15)",
  rangeSelectionHighlightColor: "hsla(263.4, 70%, 50.4%, 0.25)",
  cellHorizontalPadding: GRID_CELL_HORIZONTAL_PADDING_PX,
  rowVerticalPaddingScale: 0.9,
};

/** The grid theme for the host's current theme, at the panel's font size. */
export function buildGridTheme(theme: AppBridgeThemeInfo | null, fontSize: number) {
  const dark = theme?.resolvedDark ?? true;
  const tokens = theme?.tokens;
  return themeQuartz.withPart(dark ? colorSchemeDark : colorSchemeLight).withParams({
    ...(dark ? DARK_PARAMS : LIGHT_PARAMS),
    backgroundColor: token(tokens, "--background", dark ? "hsl(0, 0%, 3.9%)" : "hsl(0, 0%, 89.8%)"),
    foregroundColor: token(tokens, "--foreground", dark ? "hsl(0, 0%, 98%)" : "hsl(0, 0%, 3.9%)"),
    borderColor: token(tokens, "--border", dark ? "hsl(0, 0%, 14.9%)" : "hsl(0, 0%, 63.9%)"),
    fontSize,
  });
}
