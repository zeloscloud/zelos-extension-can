import { afterEach, describe, expect, it, vi } from "vitest";
import { installReducedMotionPolicy } from "../reduced-motion";

function host(userAgent: string, reduce: boolean) {
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(userAgent);
  const query = { matches: reduce, addEventListener: vi.fn(), removeEventListener: vi.fn() };
  vi.spyOn(window, "matchMedia").mockReturnValue(query as unknown as MediaQueryList);
  return query;
}

const stilled = () => document.documentElement.hasAttribute("data-reduce-motion");

afterEach(() => {
  vi.restoreAllMocks();
  document.documentElement.removeAttribute("data-reduce-motion");
});

describe("the Reduce Motion rule", () => {
  it("stills the flash when macOS reports Reduce Motion", () => {
    host("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", true);
    installReducedMotionPolicy();
    expect(stilled()).toBe(true);
  });

  // A remote desktop session reports Reduce Motion on Windows; Zelos keeps its motion there.
  it("keeps the flash where only Windows or Linux reports it", () => {
    for (const userAgent of [
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Mozilla/5.0 (X11; Linux x86_64)",
    ]) {
      host(userAgent, true);
      installReducedMotionPolicy()();
      expect(stilled()).toBe(false);
    }
  });

  it("follows the preference when it changes", () => {
    const query = host("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", false);
    installReducedMotionPolicy();
    expect(stilled()).toBe(false);
    query.matches = true;
    for (const [, listener] of query.addEventListener.mock.calls) (listener as () => void)();
    expect(stilled()).toBe(true);
  });
});
