// Built once: option resolution is not free, and a sort calls this per comparison.
const naturalCollator = new Intl.Collator(undefined, { numeric: true, sensitivity: "variant" });

/** Natural order over mixed values: `0x9` before `0x10`, numbers by value, nullish first. */
export function compareNaturalMixed(a: unknown, b: unknown): number {
  if (a === b) return 0;
  if (a == null) return -1;
  if (b == null) return 1;
  if (typeof a === "number" && typeof b === "number") {
    // Not `a - b`: a NaN would yield NaN and destabilize the sort.
    return a < b ? -1 : a > b ? 1 : 0;
  }
  return naturalCollator.compare(String(a), String(b));
}
