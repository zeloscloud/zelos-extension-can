import { existsSync, readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "src");

// One HTML document per panel: src/panels/<id>.html -> dist/panels/<id>.html at the extension root.
function documents(): Record<string, string> {
  const inputs: Record<string, string> = {};
  const panels = resolve(src, "panels");
  if (existsSync(panels)) {
    for (const file of readdirSync(panels)) {
      if (file.endsWith(".html")) inputs[`panels/${file.slice(0, -".html".length)}`] = resolve(panels, file);
    }
  }
  return inputs;
}

export default defineConfig({
  root: src,
  // Relative asset URLs, so documents load from zelos-app://<id>/~<version>/<entry>.
  base: "./",
  // Copied as-is into dist/: public/panels/<id>.options.json is each panel's options schema.
  publicDir: resolve(here, "public"),
  plugins: [react()],
  build: {
    outDir: resolve(here, "../dist"),
    emptyOutDir: true,
    // The grid registers every AG Grid Community module; the panel loads from disk, not the network.
    chunkSizeWarningLimit: 2000,
    rollupOptions: { input: documents() },
  },
  test: {
    root: here,
    environment: "happy-dom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
