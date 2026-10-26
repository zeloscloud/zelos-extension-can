import { describe, expect, it } from "vitest";
import { createSearchMatcher, extractHighlightTerms } from "../grid/search";

describe("the grid search", () => {
  it("matches text without a wildcard as a substring, ignoring case", () => {
    const matches = createSearchMatcher("dut_status");
    expect(matches("0064_DUT_Status soc = 59.5 %")).toBe(true);
    expect(matches("0064_DUT_Logging")).toBe(false);
  });

  it("reads `*` as any run of characters and `?` as one", () => {
    expect(createSearchMatcher("0x0*4")("0x064 2 00 01")).toBe(true);
    expect(createSearchMatcher("0x?64")("0x064")).toBe(true);
    expect(createSearchMatcher("0x?64")("0x0064")).toBe(false);
  });

  it("takes every other character literally", () => {
    expect(createSearchMatcher("(3)*mode")("CHARGING (3) mode")).toBe(true);
    expect(createSearchMatcher("a.c*")("abc")).toBe(false);
  });

  it("matches every row while the box is empty", () => {
    expect(createSearchMatcher("  ")("anything")).toBe(true);
  });

  it("highlights the literal runs between wildcards", () => {
    expect(extractHighlightTerms(" soc ")).toEqual(["soc"]);
    expect(extractHighlightTerms("0x0*4?ff")).toEqual(["0x0", "4", "ff"]);
  });
});
