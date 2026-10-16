/**
 * The grid's quick filter: one GLOB (`*retry?of*`), auto-wrapped in `*` so a bare word matches anywhere in
 * the row, case-insensitive. Text without a wildcard is a plain substring search.
 */

const GLOB_CHARS = /[*?]/;

export function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function createSearchMatcher(searchTerm: string): (text: string) => boolean {
  const trimmed = searchTerm.trim();
  if (!trimmed) return () => true;

  if (!GLOB_CHARS.test(trimmed)) {
    const needle = trimmed.toLowerCase();
    return (text) => text.toLowerCase().includes(needle);
  }

  const pattern = trimmed
    .split("")
    .map((char) => (char === "*" ? ".*" : char === "?" ? "." : escapeRegExp(char)))
    .join("");
  // `s`: AG Grid joins a row's columns with newlines, and a wildcard must reach across them.
  const regex = new RegExp(`^.*${pattern}.*$`, "is");
  return (text) => regex.test(text);
}

/** The literal runs of a search term — what a cell highlights, wildcards excluded. */
export function extractHighlightTerms(searchTerm: string): string[] {
  const trimmed = searchTerm.trim();
  if (!trimmed) return [];
  if (!GLOB_CHARS.test(trimmed)) return [trimmed];
  const literals = trimmed
    .split(/[*?]+/)
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
  return literals.length > 0 ? literals : [trimmed];
}
