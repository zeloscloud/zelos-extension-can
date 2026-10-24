/**
 * The panel's options, as `public/panels/bus-monitor.options.json` declares them. A layout is JSON that can
 * be hand-edited or written by an older build, so every option is re-validated on read rather than trusted.
 */

/** The persisted `options` bag, type-checked: both switches are off unless the layout holds a real `true`. */
export function resolveBusMonitorOptions(options: Record<string, unknown> | null | undefined) {
  return {
    // Highlight a byte/value that differed from the previous poll for ~300ms. Off by default: a busy bus
    // repaints most of the grid every poll, which reads as noise, not signal.
    flashChanges: options?.flashChanges === true,
    // The Source column: the table each row came from. The Message cell's hover text carries it either way.
    // Off by default: every row of a bus repeats the same path, so it costs width for little.
    showSource: options?.showSource === true,
  };
}

/** Every grid's default text size, in px. */
const DEFAULT_GRID_FONT_PX = 14;
const MIN_FONT_PX = 6;
const MAX_FONT_PX = 128;

/** The stored `fontSize` option in px, clamped so a hand-edited layout can't blow up the panel. */
export function resolveFontSizePx(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return DEFAULT_GRID_FONT_PX;
  return Math.min(MAX_FONT_PX, Math.max(MIN_FONT_PX, Math.round(value)));
}

/** A grid row's height at a font size: text plus padding, so a larger font never clips. */
export function gridRowHeight(fontSizePx: number): number {
  return Math.max(16, Math.round(fontSizePx * 1.7));
}
