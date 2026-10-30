import { beforeEach, describe, expect, it, vi } from "vitest";
import { buildCanColumnDefs, canColumnWidths } from "../columns";
import { formatTime } from "../time";

// jsdom draws nothing: a canvas stands in, its characters 0.6 em wide in the cells' mono face and
// 0.75 em in the header's weight-500 face.
const MONO_EM = 0.6;
const HEADER_EM = 0.75;
const fontPx = (font: string) => Number(/(\d+(?:\.\d+)?)px/.exec(font)?.[1]);
const cell = (text: string, px: number) => Math.ceil(text.length * px * MONO_EM) + 32;
const header = (label: string, px: number, sortable: boolean) =>
  Math.ceil(label.length * px * HEADER_EM + 24 + (sortable ? 24 : 0)) + 32;

describe("the measured column floors", () => {
  beforeEach(() => {
    const measure = {
      font: "",
      measureText(text: string) {
        const em = this.font.startsWith("500 ") ? HEADER_EM : MONO_EM;
        return { width: text.length * fontPx(this.font) * em };
      },
    };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      measure as unknown as CanvasRenderingContext2D,
    );
  });

  it("holds a whole wall-clock time, and grows with the font", () => {
    const wallClock = formatTime(1_792_613_016.123456, "absolute");

    expect(canColumnWidths("absolute", 14, 0, 0)?.timestamp).toBe(cell(wallClock, 14));
    expect(canColumnWidths("absolute", 20, 0, 0)?.timestamp).toBe(cell(wallClock, 20));
  });

  it("holds a whole elapsed time at either end of the data", () => {
    expect(canColumnWidths("relative", 20, 0, 432_000.5)?.timestamp).toBe(
      cell(formatTime(432_000.5, "relative"), 20),
    );
    expect(canColumnWidths("relative", 20, -36_000, 5)?.timestamp).toBe(
      cell(formatTime(-36_000, "relative"), 20),
    );
  });

  it("holds the Timestamp header and its icons when every time is shorter", () => {
    expect(canColumnWidths("relative", 14, 0, 5)?.timestamp).toBe(header("Timestamp", 14, true));
  });

  it.each([10, 14, 20, 28])("holds the DLC header and its filter button at %ipx", (px) => {
    expect(canColumnWidths("absolute", px, 0, 0)?.dlc).toBe(header("DLC", px, false));
  });

  it.each([10, 14, 20])("starts Message with room for a 21-character name at %ipx", (px) => {
    expect(canColumnWidths("absolute", px, 0, 0)?.message).toBe(
      Math.max(cell("0".repeat(21), px), header("Message", px, true)),
    );
  });

  it.each([10, 14, 20])("holds a classic frame's 8 bytes in the Data column at %ipx", (px) => {
    const bytes = Math.ceil(8 * 2 * px * MONO_EM + 7 * 6) + 32;
    expect(canColumnWidths("absolute", px, 0, 0)?.data).toBe(
      Math.max(bytes, header("Data", px, false)),
    );
  });

  it("starts each measured column at its width, and never narrows a floored one below", () => {
    const widths = { timestamp: 400, message: 220, dlc: 90, data: 260 };
    const [timestamp, message, dlc, data] = buildCanColumnDefs({
      flashChanges: false,
      showSource: false,
      widths,
    });

    expect([timestamp?.initialWidth, timestamp?.minWidth, timestamp?.width]).toEqual([
      400,
      400,
      undefined,
    ]);
    expect([message?.initialWidth, message?.minWidth, message?.width]).toEqual([
      220,
      undefined,
      undefined,
    ]);
    expect([dlc?.initialWidth, dlc?.minWidth, dlc?.width]).toEqual([90, 90, undefined]);
    expect([data?.initialFlex, data?.minWidth, data?.flex]).toEqual([1, 260, undefined]);
  });

  it("leaves the widths to AG Grid when nothing can measure text", () => {
    const defs = buildCanColumnDefs({ flashChanges: false, showSource: false, widths: undefined });

    for (const def of defs) expect(def.minWidth).toBeUndefined();
    expect(defs[0]?.initialWidth).toBeUndefined();
  });
});
