import { beforeEach, describe, expect, it, vi } from "vitest";
import { buildCanColumnDefs, timestampWidth } from "../columns";
import { formatTime } from "../time";

// happy-dom draws nothing: a canvas whose characters are 0.6 em wide stands in for the cells' mono face.
const CHAR_EM = 0.6;
const shown = (text: string, fontSizePx: number) => Math.ceil(text.length * fontSizePx * CHAR_EM) + 32;

describe("the Timestamp column's width", () => {
  beforeEach(() => {
    const measure = {
      font: "",
      measureText(text: string) {
        return { width: text.length * Number.parseFloat(this.font) * CHAR_EM };
      },
    };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(measure as unknown as CanvasRenderingContext2D);
  });

  it("holds a whole wall-clock time, and grows with the font", () => {
    const wallClock = formatTime(1_792_613_016.123456, "absolute");

    expect(timestampWidth("absolute", 14, 0, 0)).toBe(shown(wallClock, 14));
    expect(timestampWidth("absolute", 20, 0, 0)).toBe(shown(wallClock, 20));
  });

  it("holds a whole elapsed time at either end of the data", () => {
    expect(timestampWidth("relative", 14, 0, 432_000.5)).toBe(shown(formatTime(432_000.5, "relative"), 14));
    expect(timestampWidth("relative", 14, -36_000, 5)).toBe(shown(formatTime(-36_000, "relative"), 14));
  });

  it("is where the column starts, and it is never narrowed below it", () => {
    const [timestamp] = buildCanColumnDefs({ flashChanges: false, showSource: false, timeWidth: 400 });

    expect([timestamp?.initialWidth, timestamp?.minWidth, timestamp?.width]).toEqual([400, 400, undefined]);
  });

  it("keeps the default font's width when nothing can measure text", () => {
    const [timestamp] = buildCanColumnDefs({ flashChanges: false, showSource: false, timeWidth: undefined });

    expect(timestamp?.width).toBe(310);
  });
});
