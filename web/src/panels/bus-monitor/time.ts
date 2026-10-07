/** How a time reads: wall clock (`absolute`) or elapsed from the start of a trace (`relative`). */
export type TimeMode = "absolute" | "relative";

/** The host's time mode narrowed to the two this panel renders; anything newer reads as wall clock. */
export function toTimeMode(mode: string | null | undefined): TimeMode {
  return mode === "relative" ? "relative" : "absolute";
}

const pad = (n: number, width = 2) => Math.floor(n).toString().padStart(width, "0");

/** Nanoseconds as a string, number or bigint → seconds. Malformed or missing input is 0. */
export function nsToSec(ns: string | number | bigint | null | undefined): number {
  if (ns == null) return 0;
  if (typeof ns === "bigint") return Number(ns) / 1e9;
  if (typeof ns === "number") return ns / 1e9;
  const trimmed = ns.trim();
  if (!trimmed) return 0;
  try {
    return Number(BigInt(trimmed)) / 1e9;
  } catch {
    return 0;
  }
}

/** Elapsed time to the microsecond: `m:ss.uuuuuu`, or `h:mm:ss.uuuuuu` past an hour. */
function formatElapsed(timeS: number): string {
  const ns = timeS * 1e9;
  const sign = ns < 0 ? "-" : "";
  // Whole microseconds first, then integer arithmetic, so 0.9999995 s can't carry into "0:00.1000000".
  const totalUs = Math.round(Math.abs(ns) / 1_000);
  const h = Math.floor(totalUs / 3_600_000_000);
  const m = Math.floor((totalUs % 3_600_000_000) / 60_000_000);
  const s = Math.floor((totalUs % 60_000_000) / 1_000_000);
  const frac = pad(totalUs % 1_000_000, 6);
  return h > 0 ? `${sign}${h}:${pad(m)}:${pad(s)}.${frac}` : `${sign}${m}:${pad(s)}.${frac}`;
}

/** Local wall-clock time to the microsecond, with the zone's offset: `YYYY-MM-DDTHH:MM:SS.uuuuuu±HH:MM`. */
function formatTimestamp(timeS: number): string {
  // Integer nanoseconds, so the microseconds are cut, never rounded up into the next second.
  const ns = BigInt(Math.trunc(timeS * 1e9));
  const wholeS = ns / 1_000_000_000n;
  const micros = (ns % 1_000_000_000n) / 1_000n;
  const d = new Date(Number(wholeS * 1_000n));
  const offsetMin = -d.getTimezoneOffset();
  const offset = `${offsetMin >= 0 ? "+" : "-"}${pad(Math.abs(offsetMin) / 60)}:${pad(Math.abs(offsetMin) % 60)}`;
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${micros.toString().padStart(6, "0")}${offset}`
  );
}

/** A time in seconds as the panel's time mode renders it. */
export function formatTime(timeS: number, mode: TimeMode): string {
  if (!Number.isFinite(timeS)) return "";
  return mode === "relative" ? formatElapsed(timeS) : formatTimestamp(timeS);
}
