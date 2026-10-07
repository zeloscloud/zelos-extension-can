import { describe, expect, it } from "vitest";
import type { CanRow } from "../data";
import { canMenuCopy, canMenuItems } from "../menu";

const eventRow: CanRow = {
  id: "e",
  kind: "event",
  message: "0064_DUT_Status",
  dlc: null,
  tokens: ["soc = 59.5 %", "state = 3"],
  timeS: 10,
  timeMode: "absolute",
  source: "CAN/can0/0064_DUT_Status",
};

describe("canMenuItems", () => {
  it("offers a decoded row its value and the row, a separator between them, and no bytes", () => {
    expect(canMenuItems({ row: eventRow, cellText: "0064_DUT_Status" })).toEqual([
      { id: "copy-value", label: "Copy value", icon: "copy", separatorAfter: true },
      { id: "copy-row-json", label: "Copy row as JSON", icon: "json" },
    ]);
  });

  it("offers only the row on an empty cell, with no separator above it", () => {
    expect(canMenuItems({ row: eventRow, cellText: "" })).toEqual([
      { id: "copy-row-json", label: "Copy row as JSON", icon: "json" },
    ]);
  });

  it("copies a decoded row's Data cell as its lines, and the row as JSON", () => {
    const target = { row: eventRow, cellText: "soc = 59.5 %\nstate = 3" };
    expect(canMenuCopy("copy-value", target)).toEqual({ text: "soc = 59.5 %\nstate = 3", toast: "Value copied" });
    expect(canMenuCopy("copy-bytes", target)).toBeNull();
    expect(JSON.parse(canMenuCopy("copy-row-json", target)?.text ?? "null")).toEqual({
      source: "CAN/can0/0064_DUT_Status",
      message: "0064_DUT_Status",
      dlc: null,
      time_s: 10,
      data: ["soc = 59.5 %", "state = 3"],
    });
  });
});
