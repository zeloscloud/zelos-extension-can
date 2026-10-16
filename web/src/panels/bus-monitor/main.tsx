import type { MockBridge } from "@zeloscloud/app-extension-sdk";
import { useZelosBridge, ZelosBridgeProvider } from "@zeloscloud/app-extension-sdk/react";
import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { DEV_SIGNALS, startDevFeed } from "./dev-feed";
import { BusMonitorPanel } from "./panel";
import "../../index.css";

/** Outside Zelos (`npm run dev`), the SDK's mock host stands in; this feeds it a synthetic bus. */
function DevFeed() {
  const { bridge, mode } = useZelosBridge();
  useEffect(() => {
    if (mode !== "standalone" || !bridge) return;
    return startDevFeed(bridge as MockBridge);
  }, [bridge, mode]);
  return null;
}

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("Missing #root element");

createRoot(rootElement).render(
  <StrictMode>
    <ZelosBridgeProvider
      connectOptions={{
        extensionId: "local.zelos-extension-can",
        name: "CAN",
        panel: { panelId: "bus-monitor", instanceId: "dev-bus-monitor", signals: DEV_SIGNALS },
        workspace: { modeKind: "LIVE" },
        time: { playback: "LIVE", cursorS: null, viewRange: null },
      }}
    >
      <DevFeed />
      <BusMonitorPanel />
    </ZelosBridgeProvider>
  </StrictMode>,
);
