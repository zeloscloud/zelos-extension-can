import { ZelosBridgeProvider } from "@zeloscloud/app-extension-sdk/react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BusMonitorPanel } from "./panel";
import { installReducedMotionPolicy } from "./reduced-motion";
import "../../index.css";

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("Missing #root element");
const root = createRoot(rootElement);
installReducedMotionPolicy();

function render(dev: typeof import("./dev-feed") | null) {
  root.render(
    <StrictMode>
      <ZelosBridgeProvider connectOptions={dev?.DEV_CONNECT_OPTIONS}>
        {dev && <dev.DevFeed />}
        <BusMonitorPanel />
      </ZelosBridgeProvider>
    </StrictMode>,
  );
}

// Outside Zelos (`npm run dev`), a synthetic bus feeds the SDK's mock host; the build leaves it out.
if (import.meta.env.DEV) void import("./dev-feed").then(render);
else render(null);
