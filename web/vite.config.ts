import { existsSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const src = resolve(import.meta.dirname, "src");

// One HTML document per contribution. Vite writes each one at its path under src/:
// src/tab/index.html -> dist/tab/index.html, src/panels/<id>.html -> dist/panels/<id>.html.
function documents(): Record<string, string> {
  const inputs: Record<string, string> = {};
  const tab = resolve(src, "tab/index.html");
  if (existsSync(tab)) inputs["tab"] = tab;
  const panels = resolve(src, "panels");
  if (existsSync(panels)) {
    for (const file of readdirSync(panels)) {
      if (file.endsWith(".html"))
        inputs[`panels/${file.slice(0, -".html".length)}`] = resolve(panels, file);
    }
  }
  return inputs;
}

export default defineConfig({
  root: src,
  // Relative asset URLs, so documents load from zelos-app://<id>/~<version>/<entry>.
  base: "./",
  // Copied as-is into dist/, e.g. public/panels/<id>.options.json.
  publicDir: resolve(import.meta.dirname, "public"),
  plugins: [react()],
  resolve: {
    // One React instance, also when the SDK is linked locally (file: or npm link).
    dedupe: ["react", "react-dom"],
  },
  build: {
    outDir: resolve(import.meta.dirname, "../dist"),
    emptyOutDir: true,
    // AG Grid is most of the bundle, and the panel loads from the installed extension, not a network.
    chunkSizeWarningLimit: 2048,
    rollupOptions: { input: documents() },
  },
  test: {
    environment: "jsdom",
    setupFiles: [resolve(src, "test-setup.ts")],
  },
});
