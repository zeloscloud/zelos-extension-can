import { type AppBridgePanelSignal, MockBridge } from "@zeloscloud/app-extension-sdk";
import { useZelosBridge, ZelosBridgeProvider } from "@zeloscloud/app-extension-sdk/react";
import { act, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BusMonitorPanel } from "../panel";

const SIGNALS: AppBridgePanelSignal[] = ["arbitration_id", "is_extended", "dlc", "data"].map(
  (signal) => ({
    source: "CAN",
    message: "can0/Frame",
    eventType: "zelos.can.frame.v1",
    color: "",
    signal,
    path: `CAN/can0/Frame.${signal}`,
  }),
);

/** Mounts the panel on the real grid at `fontSize`: its mock host, and its header cells' widths. */
function mountPanel(instanceId: string, fontSize: number) {
  let bridge: MockBridge | null = null;
  function CaptureBridge() {
    const value = useZelosBridge();
    if (value.bridge instanceof MockBridge) bridge = value.bridge;
    return null;
  }
  const { container } = render(
    <ZelosBridgeProvider
      showDevelopmentBanner={false}
      connectOptions={{
        extensionId: "local.zelos-extension-can",
        panel: { panelId: "bus-monitor", instanceId, signals: SIGNALS, options: { fontSize } },
        workspace: { id: "ws", modeKind: "LIVE" },
        time: { playback: "LIVE", cursorS: null, viewRange: null },
      }}
    >
      <CaptureBridge />
      <BusMonitorPanel />
    </ZelosBridgeProvider>,
  );
  return {
    bridge: () => bridge,
    headerWidth: (colId: string) =>
      container.querySelector<HTMLElement>(`.ag-header-cell[col-id="${colId}"]`)?.style.width,
  };
}

beforeEach(() => {
  // jsdom draws nothing: characters measure 0.6 em in the mono face and 0.75 em in the header's.
  const measure = {
    font: "",
    measureText(text: string) {
      const px = Number(/(\d+(?:\.\d+)?)px/.exec(this.font)?.[1]);
      return { width: text.length * px * (this.font.startsWith("500 ") ? 0.75 : 0.6) };
    },
  };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
    measure as unknown as CanvasRenderingContext2D,
  );
});

afterEach(() => sessionStorage.clear());

describe("a column the user resized", () => {
  it("keeps its width when the panel's font changes", async () => {
    sessionStorage.setItem(
      "zelos.panel.ws/resized.grid",
      JSON.stringify({ quickFilterText: "", columnState: [{ colId: "message", width: 400 }] }),
    );
    const { bridge, headerWidth } = mountPanel("resized", 14);
    await waitFor(() => expect(headerWidth("message")).toBe("400px"));
    const timestampAt14 = headerWidth("timestamp");

    act(() => bridge()?.setPanel({ options: { fontSize: 20 } }));

    await waitFor(() => expect(headerWidth("timestamp")).not.toBe(timestampAt14));
    expect(headerWidth("message")).toBe("400px");
  });
});

describe("at a large font", () => {
  it("keeps the DLC header's label and filter button whole", async () => {
    const { headerWidth } = mountPanel("large-font", 20);

    // "DLC" at 20 px (45), 8 px from the filter button (16), inside the cell padding (2 × 16).
    await waitFor(() => expect(headerWidth("dlc")).toBe("101px"));
  });

  it("starts Message wide enough for a 21-character name", async () => {
    const { headerWidth } = mountPanel("large-font-message", 20);

    // 21 mono characters at 20 px (252), inside the cell padding (2 × 16).
    await waitFor(() => expect(headerWidth("message")).toBe("284px"));
  });
});
