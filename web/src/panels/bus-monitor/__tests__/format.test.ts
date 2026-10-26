import { describe, expect, it } from "vitest";
import { formatLatestValue } from "../use-bus-data";

describe("formatLatestValue", () => {
  const states = { "0": "OFF", "3": "CHARGING" };

  // The code stays beside its name, so a reader can still match it to a frame's bytes.
  it("reads a value-table hit as `LABEL (n)`, unit after it", () => {
    expect(formatLatestValue("3", { valueTable: states })).toBe("CHARGING (3)");
    expect(formatLatestValue("3", { valueTable: states, unit: "mode" })).toBe("CHARGING (3) mode");
  });

  it("shows the value itself when the table has no entry for it", () => {
    expect(formatLatestValue("7", { valueTable: states })).toBe("7");
  });

  it("shows the value itself, with its unit, when the signal has no table", () => {
    expect(formatLatestValue("59.5", { valueTable: null, unit: "%" })).toBe("59.5 %");
    expect(formatLatestValue("59.500000001", { valueTable: null, unit: "%" })).toBe("59.5 %");
  });

  it("shows a dictionary value's table label alone, and an empty value as a dash", () => {
    expect(formatLatestValue("3", { valueTable: states, dictionary: true })).toBe("CHARGING");
    expect(formatLatestValue("", { valueTable: states })).toBe("-");
  });

  // A tiny float keeps its exponent form instead of collapsing to 0; others keep at most 8 fraction digits.
  it("shows a float in its shortest form, an exponent form as is", () => {
    expect(formatLatestValue("0.0000001234567891", undefined)).toBe("1.234567891e-7");
    expect(formatLatestValue("0.00000000012345", undefined)).toBe("1.2345e-10");
    expect(formatLatestValue("1.123456789", undefined)).toBe("1.12345679");
  });

  // A 64-bit integer goes nowhere near a JS number, which would round its low digits.
  it("shows an integer's text verbatim", () => {
    expect(formatLatestValue("18446744073709551615", undefined)).toBe("18446744073709551615");
  });

  // An inherited member is not a label.
  it("ignores keys the table only inherits", () => {
    expect(formatLatestValue("constructor", { valueTable: states })).toBe("constructor");
  });
});
