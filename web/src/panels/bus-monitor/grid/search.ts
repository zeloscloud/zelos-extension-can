// One case-insensitive glob (`*retry?of*`) that matches anywhere in the row; without a wildcard, a substring.

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
  const regex = new RegExp(`^.*${pattern}.*$`, "i");
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
