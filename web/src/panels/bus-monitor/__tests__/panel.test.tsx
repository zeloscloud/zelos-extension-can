import {
  type AppBridgePanelSignal,
  type ColumnMetadata,
  MockBridge,
  type PanelSubscribeParams,
  type QueryDataMulti,
} from "@zeloscloud/app-extension-sdk";
import { useZelosBridge, ZelosBridgeProvider } from "@zeloscloud/app-extension-sdk/react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ColDef } from "ag-grid-community";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type CanRow, canRowHeight } from "../data";
import { forgetLatches, latchKey } from "../latch";
import { gridRowHeight, resolveFontSizePx } from "../options";
import { BusMonitorPanel, EMPTY_MESSAGE } from "../panel";

/** The props the panel last handed the grid, for driving its callbacks with a stand-in api. */
const grid = vi.hoisted(() => ({ props: {} as Record<string, unknown> }));

// AG Grid's own rendering needs real layout. The stand-in renders rows and cells with the same classes and
// attributes the panel's context menu reads.
vi.mock("ag-grid-react", () => ({
  AgGridReact: (props: { rowData?: CanRow[]; columnDefs?: ColDef<CanRow>[] }) => {
    grid.props = props;
    const { rowData, columnDefs } = props;
    return (
      <div data-testid="grid">
        {rowData?.map((row) => (
          <div key={row.id} className="ag-row" row-id={row.id}>
            {columnDefs?.map((column) => {
              const getter = column.valueGetter;
              const value = typeof getter === "function" ? getter({ data: row } as never) : "";
              return (
                <div key={column.colId} className="ag-cell" col-id={column.colId}>
                  {String(value ?? "")}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    );
  },
}));

const FRAME = { source: "CAN", message: "can0/Frame", eventType: "zelos.can.frame.v1", color: "" };
const FRAME_SIGNALS: AppBridgePanelSignal[] = ["arbitration_id", "is_extended", "dlc", "data"].map((signal) => ({
  ...FRAME,
  signal,
  path: `CAN/can0/Frame.${signal}`,
}));

const column = (signal: string, producer: string | null = null): ColumnMetadata => ({
  source: signal === "" ? "time_s" : "CAN",
  message: signal === "" ? "" : "can0/Frame",
  signal,
  producer: signal === "" ? null : producer,
  tracePath: null,
  dataSegmentId: null,
  startTimeS: null,
  endTimeS: null,
});

/** One frame window, newest first: id 0x064 at `timeS` carrying `data`, and 0x12C before it. */
function frameWindow(timeS: number, data: string, producer: string | null = null) {
  const dataset: QueryDataMulti = {
    columns: ["", "arbitration_id", "is_extended", "dlc", "data"].map((signal) => column(signal, producer)),
    data: [
      [timeS, timeS - 1],
      [0x064, 0x12c],
      [false, false],
      [2, 2],
      [data, "0xdead"],
    ],
    range: null,
    queryDurationS: 0,
  };
  return { id: "frames", dataset, latest: null, timeMode: "absolute", totalRows: 2, isLoading: false, error: null };
}

type Call = { method: string; params: unknown };

/** Mounts the panel against the SDK's mock host, and hands back the mock and every call the panel made. */
async function mountPanel(menuChoice: string | null = null, signals: AppBridgePanelSignal[] = FRAME_SIGNALS) {
  const calls: Call[] = [];
  let bridge: MockBridge | null = null;

  function CaptureBridge() {
    const value = useZelosBridge();
    if (value.bridge instanceof MockBridge && bridge !== value.bridge) {
      bridge = value.bridge;
      bridge.setInvokeHandler((method, params) => {
        calls.push({ method, params });
        if (method === "panel.subscribe") return { id: (params as { id: string }).id };
        if (method === "panel.showMenu") return { itemId: menuChoice };
        return null;
      });
    }
    return null;
  }

  render(
    <ZelosBridgeProvider
      showDevelopmentBanner={false}
      connectOptions={{
        extensionId: "local.zelos-extension-can",
        panel: { panelId: "bus-monitor", instanceId: "test-panel", signals },
        workspace: { id: "ws", modeKind: "TRACE" },
        time: { playback: "PAUSED", cursorS: 100, viewRange: { startS: 0, endS: 100 } },
      }}
    >
      <CaptureBridge />
      <BusMonitorPanel />
    </ZelosBridgeProvider>,
  );

  await waitFor(() => expect(calls.some((call) => call.method === "panel.subscribe")).toBe(true));
  if (!bridge) throw new Error("the mock bridge never connected");
  return { bridge: bridge as MockBridge, calls };
}

const messageCells = () => document.querySelectorAll('.ag-cell[col-id="message"]');

/** A mounted panel that has latched the two rows of one frame window at 90 s. */
async function mountWithFrames() {
  const mounted = await mountPanel();
  act(() => mounted.bridge.pushData(frameWindow(90, "0x0001")));
  await waitFor(() => expect(messageCells()).toHaveLength(2));
  return mounted;
}

beforeEach(() => {
  sessionStorage.clear();
  forgetLatches();
});

afterEach(() => {
  cleanup();
});

describe("BusMonitorPanel", () => {
  it("subscribes to the frame window with one deep first read, ending at the cursor", async () => {
    const { calls } = await mountPanel();
    const subscribe = calls.find((call) => call.method === "panel.subscribe");
    expect(subscribe?.params).toEqual({
      id: "frames",
      shape: "rows",
      signals: FRAME_SIGNALS.map((signal) => signal.path),
      maxRows: 100_000,
      sortOrder: "desc",
      endAtCursor: true,
      minPollMs: 200,
    });
    // Frames only: there is nothing for a latest-value subscription to read.
    expect(calls.filter((call) => call.method === "panel.subscribe").map((call) => (call.params as { id: string }).id))
      .not.toContain("latest");
  });

  // The host refuses more than a million cells: 84 buses of 4 fields at 100,000 rows would be 33.6 million.
  it("fits the frame window to the host's cell cap, and says so in the window status", async () => {
    const buses = Array.from({ length: 84 }, (_, bus) =>
      FRAME_SIGNALS.map((signal) => ({
        ...signal,
        message: `can${bus}/Frame`,
        path: `CAN/can${bus}/Frame.${signal.signal}`,
      })),
    ).flat();
    const { bridge, calls } = await mountPanel(null, buses);
    const subscribe = calls.find((call) => call.method === "panel.subscribe")?.params as PanelSubscribeParams;
    expect(subscribe.signals).toHaveLength(336);
    expect(subscribe.maxRows).toBe(2_976);

    act(() => bridge.pushData({ ...frameWindow(90, "0x0001"), totalRows: 5_000 }));
    await waitFor(() =>
      expect(screen.getByTestId("can-window-status").textContent).toBe("window: newest 2,976 of 5,000 rows"),
    );
  });

  it("renders one latched row per id from pushed frames, and persists them", async () => {
    await mountWithFrames();
    expect([...messageCells()].map((cell) => cell.textContent).sort()).toEqual(["0x064", "0x12C"]);
    const stored = JSON.parse(sessionStorage.getItem(latchKey("test-panel")) ?? "null");
    expect(stored.rows).toHaveLength(2);
  });

  it("clears the latch when the cursor moves back", async () => {
    const { bridge } = await mountWithFrames();

    act(() => bridge.setTime({ cursorS: 50 }));
    await waitFor(() => expect(messageCells()).toHaveLength(0));

    // The window for the new cursor lands and refills the latch.
    act(() => bridge.pushData(frameWindow(40, "0x0002")));
    await waitFor(() => expect(messageCells()).toHaveLength(2));
  });

  // A seek's new window can land before the panel notices the cursor moved back (the cursor is held to the
  // poll rate). The rows must refill from it, with nothing else arriving.
  it("refills after a seek back whose new window landed first", async () => {
    const { bridge } = await mountWithFrames();

    act(() => bridge.setTime({ cursorS: 95 }));
    act(() => bridge.setTime({ cursorS: 50 }));
    act(() => bridge.pushData(frameWindow(40, "0x0002")));
    await waitFor(() => expect(document.querySelector('.ag-cell[col-id="data"]')?.textContent).toBe("00 02"));
    expect(messageCells()).toHaveLength(2);
  });

  // The latch scope is the workspace and its mode, never the bound signals' producers or traces.
  it("keeps the rows when a signal from another producer joins, and clears them when a frame path leaves", async () => {
    const fromP1 = FRAME_SIGNALS.map((signal) => ({ ...signal, producer: "p1" }));
    const { bridge } = await mountPanel(null, fromP1);
    act(() => bridge.pushData(frameWindow(90, "0x0001", "p1")));
    await waitFor(() => expect(messageCells()).toHaveLength(2));

    const fromP2: AppBridgePanelSignal = {
      source: "CAN",
      message: "can1/0064_DUT_Status",
      signal: "state",
      path: "CAN/can1/0064_DUT_Status.state",
      producer: "p2",
      eventType: "zelos.can.message.v1",
      color: "",
    };
    act(() => bridge.setPanel({ signals: [...fromP1, fromP2] }));
    await act(() => new Promise((resolve) => setTimeout(resolve, 100)));
    expect(messageCells()).toHaveLength(2);

    act(() => bridge.setPanel({ signals: [...fromP1.filter((signal) => signal.signal !== "data"), fromP2] }));
    await waitFor(() => expect(messageCells()).toHaveLength(0));
  });

  it("copies a frame's bytes through the host from the context menu", async () => {
    const { bridge, calls } = await mountPanel("copy-bytes");
    act(() => bridge.pushData(frameWindow(90, "0x00ff")));
    await waitFor(() => expect(messageCells()).toHaveLength(2));

    const cell = [...messageCells()].find((element) => element.textContent === "0x064");
    if (!cell) throw new Error("no row for 0x064");
    fireEvent.contextMenu(cell, { clientX: 12, clientY: 34 });

    await waitFor(() => expect(calls.some((call) => call.method === "panel.copyText")).toBe(true));
    const menu = calls.find((call) => call.method === "panel.showMenu")?.params as {
      x: number;
      y: number;
      items: unknown[];
    };
    expect(menu.x).toBe(12);
    expect(menu.y).toBe(34);
    expect(menu.items).toEqual([
      { id: "copy-value", label: "Copy value", icon: "copy" },
      { id: "copy-bytes", label: "Copy bytes", icon: "bytes", separatorAfter: true },
      { id: "copy-row-json", label: "Copy row as JSON", icon: "json" },
    ]);
    expect(calls.find((call) => call.method === "panel.copyText")?.params).toEqual({
      text: "00 FF",
      toast: "Bytes copied",
    });
  });

  it("resizes only the rows whose height changed when the data updates", async () => {
    await mountWithFrames();
    const base = gridRowHeight(resolveFontSizePx(undefined));
    const node = (data: Partial<CanRow>) => ({ data: data as CanRow, rowHeight: base, setRowHeight: vi.fn() });
    const frame = node({ kind: "frame", tokens: ["00", "01"] });
    const grown = node({ kind: "event", tokens: ["state = 0", "soc = 80 %", "mode = 2"] });
    const api = {
      isDestroyed: () => false,
      forEachNode: (visit: (n: ReturnType<typeof node>) => void) => [frame, grown].forEach(visit),
      onRowHeightChanged: vi.fn(),
    };
    const props = grid.props as { onGridReady: (event: unknown) => void; onRowDataUpdated: () => void };
    props.onGridReady({ api });

    props.onRowDataUpdated();
    expect(frame.setRowHeight).not.toHaveBeenCalled();
    expect(grown.setRowHeight).toHaveBeenCalledWith(canRowHeight(grown.data, base));
    expect(api.onRowHeightChanged).toHaveBeenCalledTimes(1);

    // Heights already right: nothing is resized and the grid is not asked to re-place its rows.
    grown.rowHeight = canRowHeight(grown.data, base);
    props.onRowDataUpdated();
    expect(grown.setRowHeight).toHaveBeenCalledTimes(1);
    expect(api.onRowHeightChanged).toHaveBeenCalledTimes(1);
  });

  it("shows a failed read as the error state, with its cause", async () => {
    const { bridge } = await mountPanel();
    act(() => bridge.pushData({ ...frameWindow(90, "0x0001"), dataset: null, error: "query timed out" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Error loading data");
    expect(alert.textContent).toContain("query timed out");
  });

  it("asks for a drop while nothing is bound", async () => {
    render(
      <ZelosBridgeProvider showDevelopmentBanner={false} connectOptions={{ panel: { signals: [] } }}>
        <BusMonitorPanel />
      </ZelosBridgeProvider>,
    );
    expect(await screen.findByText(EMPTY_MESSAGE)).toBeTruthy();
  });
});
