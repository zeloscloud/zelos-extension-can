import { describe, expect, it } from "vitest";
import { formatTime, nsToSec } from "../time";

describe("formatTime", () => {
  it("writes an elapsed time to the microsecond, hours only past an hour", () => {
    expect(formatTime(65.5, "relative")).toBe("1:05.500000");
    expect(formatTime(3725.000042, "relative")).toBe("1:02:05.000042");
    expect(formatTime(-1.25, "relative")).toBe("-0:01.250000");
  });

  // Rounded to whole microseconds first, so the fraction never reads 1000000.
  it("carries a rounded elapsed microsecond into the second", () => {
    expect(formatTime(0.9999996, "relative")).toBe("0:01.000000");
  });

  it("writes a wall-clock time with six microsecond digits, cut rather than rounded, and the zone's offset", () => {
    const wallClock = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:36\.(\d{6})[+-]\d{2}:\d{2}$/;
    expect(formatTime(1_792_613_016.0000425, "absolute").match(wallClock)?.[1]).toBe("000042");
    expect(formatTime(1_792_613_016.9999995, "absolute").match(wallClock)?.[1]).toBe("999999");
  });

  it("writes nothing for a time that is not a number", () => {
    expect(formatTime(Number.NaN, "absolute")).toBe("");
  });
});

describe("nsToSec", () => {
  it("reads nanoseconds from a string, number or bigint, and malformed input as 0", () => {
    expect(nsToSec("1500000000")).toBe(1.5);
    expect(nsToSec(2_500_000_000)).toBe(2.5);
    expect(nsToSec(3_000_000_000n)).toBe(3);
    expect(nsToSec("12.5")).toBe(0);
    expect(nsToSec(null)).toBe(0);
  });
});
