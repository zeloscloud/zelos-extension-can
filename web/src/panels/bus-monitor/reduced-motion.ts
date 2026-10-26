const QUERY = "(prefers-reduced-motion: reduce)";

/** Mark `<html>` with `data-reduce-motion` as Zelos does, and keep it in sync. Returns the stop function. */
export function installReducedMotionPolicy(): () => void {
  const query = window.matchMedia(QUERY);
  // Only macOS reports Reduce Motion by choice; Windows and Linux report it for remote and unaccelerated sessions.
  const sync = () =>
    document.documentElement.toggleAttribute(
      "data-reduce-motion",
      navigator.userAgent.includes("Macintosh") && query.matches,
    );
  query.addEventListener("change", sync);
  sync();
  return () => query.removeEventListener("change", sync);
}
