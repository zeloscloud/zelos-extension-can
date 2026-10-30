import {
  type AppBridgePanelSignal,
  type AppBridgeTimeInfo,
  type AppBridgeWorkspaceInfo,
  type ColumnMetadata,
  fitRowBudget,
  type LatestSignalValue,
  MockBridge,
  type PanelSubscribeParams,
  type QueryDataMulti,
} from "@zeloscloud/app-extension-sdk";
import { useZelosBridge, ZelosBridgeProvider } from "@zeloscloud/app-extension-sdk/react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ColDef } from "ag-grid-community";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import schema from "../../../../public/panels/bus-monitor.options.json";
import { type CanRow, canRowHeight, reduceCanFrameRows } from "../data";
import { forgetLatches, latchKey } from "../latch";
import { gridRowHeight, resolveFontSizePx } from "../options";
import { BusMonitorPanel, EMPTY_MESSAGE } from "../panel";

/** The props the panel last handed the grid, for driving its callbacks with a stand-in api. */
const grid = vi.hoisted(() => ({ props: {} as Record<string, unknown> }));

// AG Grid's own rendering needs real layout; the stand-in renders the classes and attributes the menu reads.
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

vi.mock("../data", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../data")>();
  return { ...actual, reduceCanFrameRows: vi.fn(actual.reduceCanFrameRows) };
});

const FRAME = { source: "CAN", message: "can0/Frame", eventType: "zelos.can.frame.v1", color: "" };
const FRAME_SIGNALS: AppBridgePanelSignal[] = ["arbitration_id", "is_extended", "dlc", "data"].map(
  (signal) => ({
    ...FRAME,
    signal,
    path: `CAN/can0/Frame.${signal}`,
  }),
);

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
    columns: ["", "arbitration_id", "is_extended", "dlc", "data"].map((signal) =>
      column(signal, producer),
    ),
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
  return {
    id: "frames",
    dataset,
    latest: null,
    timeMode: "absolute",
    totalRows: 2,
    isLoading: false,
    error: null,
  };
}

type Call = { method: string; params: unknown };

interface Host {
  workspace?: Partial<AppBridgeWorkspaceInfo>;
  time?: Partial<AppBridgeTimeInfo>;
}

const workspaceReading = (modeKind: string, ...traces: string[]) => ({
  id: "ws",
  name: "",
  modeKind,
  traces,
});

const PAUSED_TRACE: Required<Host> = {
  workspace: { id: "ws", modeKind: "TRACE" },
  time: { playback: "PAUSED", cursorS: 100, viewRange: { startS: 0, endS: 100 } },
};

/** Mounts the panel against the SDK's mock host, and hands back the mock and every call the panel made. */
async function mountPanel(
  menuChoice: string | null = null,
  signals: AppBridgePanelSignal[] = FRAME_SIGNALS,
  options: Record<string, unknown> | null = null,
  host: Host = {},
) {
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
        panel: { panelId: "bus-monitor", instanceId: "test-panel", signals, options },
        workspace: host.workspace ?? PAUSED_TRACE.workspace,
        time: host.time ?? PAUSED_TRACE.time,
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
    expect(
      calls
        .filter((call) => call.method === "panel.subscribe")
        .map((call) => (call.params as { id: string }).id),
    ).not.toContain("latest");
  });

  // The host cuts each bus's rows to fit a million cells; the fit keeps the request and the status line equal to it.
  it("fits the frame window to the host's cell cap, and says so in the window status", async () => {
    const buses = Array.from({ length: 84 }, (_, bus) =>
      FRAME_SIGNALS.map((signal) => ({
        ...signal,
        message: `can${bus}/Frame`,
        path: `CAN/can${bus}/Frame.${signal.signal}`,
      })),
    ).flat();
    const { bridge, calls } = await mountPanel(null, buses);
    const subscribe = calls.find((call) => call.method === "panel.subscribe")
      ?.params as PanelSubscribeParams;
    expect(subscribe.signals).toHaveLength(336);
    expect(subscribe.maxRows).toBe(2_976);

    act(() => bridge.pushData({ ...frameWindow(90, "0x0001"), totalRows: 5_000 }));
    await waitFor(() =>
      expect(screen.getByTestId("can-window-status").textContent).toBe(
        "window: newest 2,976 of 5,000 rows",
      ),
    );
  });

  it("renders one latched row per id from pushed frames, and persists them", async () => {
    await mountWithFrames();
    expect([...messageCells()].map((cell) => cell.textContent).sort()).toEqual(["0x064", "0x12C"]);
    const stored = JSON.parse(sessionStorage.getItem(latchKey("ws/test-panel")) ?? "null");
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

  // The cursor is held to the poll rate, so a seek's new window can land before the panel sees the retreat.
  it("refills after a seek back whose new window landed first", async () => {
    const { bridge } = await mountWithFrames();

    act(() => bridge.setTime({ cursorS: 95 }));
    act(() => bridge.setTime({ cursorS: 50 }));
    act(() => bridge.pushData(frameWindow(40, "0x0002")));
    await waitFor(() =>
      expect(document.querySelector('.ag-cell[col-id="data"]')?.textContent).toBe("00 02"),
    );
    expect(messageCells()).toHaveLength(2);
  });

  // The latch scope is the workspace, its mode and its recordings, never the bound signals' producers or traces.
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

    act(() =>
      bridge.setPanel({
        signals: [...fromP1.filter((signal) => signal.signal !== "data"), fromP2],
      }),
    );
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
    const fontSize = resolveFontSizePx(undefined);
    const base = gridRowHeight(fontSize);
    const node = (data: Partial<CanRow>) => ({
      data: data as CanRow,
      rowHeight: base,
      setRowHeight: vi.fn(),
    });
    const frame = node({ kind: "frame", tokens: ["00", "01"] });
    const grown = node({ kind: "event", tokens: ["state = 0", "soc = 80 %", "mode = 2"] });
    const api = {
      isDestroyed: () => false,
      forEachNode: (visit: (n: ReturnType<typeof node>) => void) => [frame, grown].forEach(visit),
      onRowHeightChanged: vi.fn(),
    };
    const props = grid.props as {
      onGridReady: (event: unknown) => void;
      onRowDataUpdated: () => void;
    };
    props.onGridReady({ api });

    props.onRowDataUpdated();
    expect(frame.setRowHeight).not.toHaveBeenCalled();
    expect(grown.setRowHeight).toHaveBeenCalledWith(canRowHeight(grown.data, base, fontSize));
    expect(api.onRowHeightChanged).toHaveBeenCalledTimes(1);

    // Heights already right: nothing is resized and the grid is not asked to re-place its rows.
    grown.rowHeight = canRowHeight(grown.data, base, fontSize);
    props.onRowDataUpdated();
    expect(grown.setRowHeight).toHaveBeenCalledTimes(1);
    expect(api.onRowHeightChanged).toHaveBeenCalledTimes(1);
  });

  it("shows a failed read as the error state, with its cause", async () => {
    const { bridge } = await mountPanel();
    act(() =>
      bridge.pushData({ ...frameWindow(90, "0x0001"), dataset: null, error: "query timed out" }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Error loading data");
    expect(alert.textContent).toContain("query timed out");
  });

  // Layouts saved by Zelos 26.0.9 and earlier refer to this panel by these option keys.
  it("reads every option a saved layout holds", async () => {
    expect(Object.keys(schema.properties)).toEqual(
      expect.arrayContaining(["flashChanges", "showSource", "fontSize"]),
    );
    // Whole px; null is the default size.
    expect(schema.properties.fontSize.type).toBe("integer");
    expect(resolveFontSizePx(null)).toBe(resolveFontSizePx(undefined));

    const filterModel = { message: { filterType: "text", type: "contains", filter: "0x064" } };
    const saved = { flashChanges: true, showSource: true, fontSize: 24, grid: { filterModel } };
    await mountPanel(null, FRAME_SIGNALS, saved);
    const props = grid.props as {
      columnDefs: ColDef<CanRow>[];
      rowHeight: number;
      onGridReady: (event: unknown) => void;
    };
    const column = (colId: string) => props.columnDefs.find((def) => def.colId === colId);
    expect(column("data")?.cellRendererParams).toEqual({ flash: true });
    expect(column("source")?.hide).toBe(false);
    expect(props.rowHeight).toBe(gridRowHeight(24));

    const setFilterModel = vi.fn();
    props.onGridReady({ api: { setFilterModel } });
    expect(setFilterModel).toHaveBeenCalledWith(filterModel);
  });

  it("asks for a drop while nothing is bound", async () => {
    render(
      <ZelosBridgeProvider
        showDevelopmentBanner={false}
        connectOptions={{ panel: { signals: [] } }}
      >
        <BusMonitorPanel />
      </ZelosBridgeProvider>,
    );
    expect(await screen.findByText(EMPTY_MESSAGE)).toBeTruthy();
  });

  it("clears the rows when the workspace changes mode", async () => {
    const { bridge } = await mountWithFrames();
    act(() => bridge.setWorkspace(workspaceReading("TRACEPATH")));
    await waitFor(() => expect(messageCells()).toHaveLength(0));
  });

  it("clears the rows when a recording closes, and keeps them while the same recordings stay open", async () => {
    const { bridge } = await mountPanel();
    act(() => bridge.setWorkspace(workspaceReading("TRACE", "/data/a.trz", "/data/b.trz")));
    act(() => bridge.pushData(frameWindow(90, "0x0001")));
    await waitFor(() => expect(messageCells()).toHaveLength(2));

    act(() => bridge.setWorkspace(workspaceReading("TRACE", "/data/a.trz", "/data/b.trz")));
    await act(() => new Promise((resolve) => setTimeout(resolve, 100)));
    expect(messageCells()).toHaveLength(2);

    act(() => bridge.setWorkspace(workspaceReading("TRACE", "/data/a.trz")));
    await waitFor(() => expect(messageCells()).toHaveLength(0));
  });

  // One saved layout seeds many workspaces with the same panel ids.
  it("keeps a panel's search and latch apart in another workspace with the same panel id", async () => {
    const subscribedRows = (calls: Call[]) =>
      (
        calls.filter((call) => call.method === "panel.subscribe").at(-1)
          ?.params as PanelSubscribeParams
      ).maxRows;
    const search = () => screen.getByLabelText<HTMLInputElement>("Search");

    const first = await mountWithFrames();
    fireEvent.change(search(), { target: { value: "0x064" } });
    await waitFor(() => expect(subscribedRows(first.calls)).toBe(10_000));
    cleanup();
    forgetLatches();

    const other = await mountPanel(null, FRAME_SIGNALS, null, {
      workspace: { id: "ws2", modeKind: "TRACE" },
    });
    expect(search().value).toBe("");
    expect(subscribedRows(other.calls)).toBe(100_000);
    cleanup();
    forgetLatches();

    const again = await mountPanel();
    expect(search().value).toBe("0x064");
    expect(subscribedRows(again.calls)).toBe(10_000);
    expect(messageCells()).toHaveLength(2);
  });

  it("restores the column layout a reload left", async () => {
    await mountPanel();
    const columnState = [{ colId: "message", width: 300, sort: "desc" as const }];
    const props = grid.props as {
      onGridReady: (event: unknown) => void;
      onSortChanged: () => void;
    };
    props.onGridReady({ api: { isDestroyed: () => false, getColumnState: () => columnState } });
    act(() => props.onSortChanged());
    cleanup();

    await mountPanel();
    const applyColumnState = vi.fn();
    (grid.props as { onGridReady: (event: unknown) => void }).onGridReady({
      api: { applyColumnState },
    });
    expect(applyColumnState).toHaveBeenCalledWith({ state: columnState, applyOrder: true });
  });

  describe("saving a filter to the layout", () => {
    const model = { message: { filterType: "text", type: "contains", filter: "0x064" } };
    const saved = (calls: Call[]) =>
      calls.filter((call) => call.method === "panel.setOptions").map((call) => call.params);

    async function editFilter() {
      const mounted = await mountPanel();
      const props = grid.props as {
        onGridReady: (event: unknown) => void;
        onFilterChanged: () => void;
      };
      props.onGridReady({ api: { isDestroyed: () => false, getFilterModel: () => model } });
      props.onFilterChanged();
      return mounted;
    }

    it("writes the model under `grid.filterModel` once the edits settle", async () => {
      const { calls } = await editFilter();
      expect(saved(calls)).toEqual([]);
      await waitFor(() =>
        expect(saved(calls)).toEqual([{ patch: { grid: { filterModel: model } } }]),
      );
    });

    it("writes a pending edit when the panel unmounts", async () => {
      const { calls } = await editFilter();
      cleanup();
      expect(saved(calls)).toEqual([{ patch: { grid: { filterModel: model } } }]);
    });

    // A frame that unloads runs no React cleanup.
    it("writes a pending edit when the frame unloads", async () => {
      const { calls } = await editFilter();
      window.dispatchEvent(new Event("pagehide"));
      expect(saved(calls)).toEqual([{ patch: { grid: { filterModel: model } } }]);
    });
  });

  it("ignores a saved filter model the grid cannot apply", async () => {
    for (const filterModel of [[{ filterType: "text" }], { message: "0x064" }]) {
      await mountPanel(null, FRAME_SIGNALS, { grid: { filterModel } });
      const setFilterModel = vi.fn();
      (grid.props as { onGridReady: (event: unknown) => void }).onGridReady({
        api: { setFilterModel },
      });
      expect(setFilterModel).not.toHaveBeenCalled();
      cleanup();
    }
  });

  it("matches a plain search across columns and decoded lines", async () => {
    await mountPanel();
    const matches = grid.props.quickFilterMatcher as (parts: string[], rowText: string) => boolean;
    const decoded = "2026-10-25T10:00:00.000000+00:00\n0064_DUT_Status\n-\nsoc = 59.5 %\nstate = 3";
    expect(matches(["soc = 59.5 % state"], decoded)).toBe(true);
    expect(matches(["0x064 2"], "2026-10-25T10:00:00.000000+00:00\n0x064\n2\n00 01")).toBe(true);
    expect(matches(["soc*state = 3"], decoded)).toBe(true);
  });

  it("fits the frame window to every bound signal, two producers of one bus counting twice", async () => {
    const buses = Array.from({ length: 40 }, (_, bus) =>
      ["p1", "p2"].flatMap((producer) =>
        FRAME_SIGNALS.map((signal) => ({
          ...signal,
          producer,
          message: `can${bus}/Frame`,
          path: `CAN/can${bus}/Frame.${signal.signal}`,
        })),
      ),
    ).flat();
    const { calls } = await mountPanel(null, buses);
    const subscribe = calls.find((call) => call.method === "panel.subscribe")
      ?.params as PanelSubscribeParams;
    expect(subscribe.signals).toHaveLength(160);
    expect(subscribe.maxRows).toBe(fitRowBudget(320, 100_000));
  });

  it("reads every other bound table from one latest-value subscription, a row per table", async () => {
    const status = {
      source: "CAN",
      message: "can0/0064_DUT_Status",
      eventType: "zelos.can.message.v1",
      color: "",
    };
    const field = (
      signal: string,
      producer: string,
      unit: string | null = null,
    ): AppBridgePanelSignal => ({
      ...status,
      signal,
      producer,
      unit,
      path: `CAN/can0/0064_DUT_Status.${signal}`,
    });
    const { bridge, calls } = await mountPanel(null, [
      ...FRAME_SIGNALS,
      field("state", "p1"),
      field("state", "p2"),
      field("soc", "p1", "%"),
    ]);
    const latest = calls.find(
      (call) =>
        call.method === "panel.subscribe" && (call.params as { id: string }).id === "latest",
    );
    expect(latest?.params).toEqual({
      id: "latest",
      shape: "latest",
      signals: ["CAN/can0/0064_DUT_Status.state", "CAN/can0/0064_DUT_Status.soc"],
      minPollMs: 200,
    });

    const value = (signal: string, text: string): LatestSignalValue => ({
      source: "CAN",
      message: "can0/0064_DUT_Status",
      signal,
      value: text,
      producer: "p1",
      tracePath: null,
      timeNs: "90000000000",
      dataSegmentId: null,
    });
    act(() =>
      bridge.pushData({
        id: "latest",
        dataset: null,
        latest: [value("soc", "59.5"), value("state", "3")],
        timeMode: "absolute",
        totalRows: 0,
        isLoading: false,
        error: null,
      }),
    );
    await waitFor(() =>
      expect([...messageCells()].map((cell) => cell.textContent)).toEqual(["0064_DUT_Status"]),
    );
    expect(document.querySelector('.ag-cell[col-id="data"]')?.textContent).toBe(
      "state = 3\nsoc = 59.5 %",
    );
  });

  // The host publishes time every animation frame while it follows live.
  it("does not reduce the frame window again for a live time push that brings no new frames", async () => {
    const { bridge } = await mountPanel(null, FRAME_SIGNALS, null, {
      workspace: { id: "ws", modeKind: "LIVE" },
      time: {
        playback: "LIVE",
        cursorS: null,
        viewRange: null,
        dataRange: { startS: 0, endS: 100 },
      },
    });
    act(() => bridge.pushData(frameWindow(90, "0x0001")));
    await waitFor(() => expect(messageCells()).toHaveLength(2));
    const reduced = vi.mocked(reduceCanFrameRows).mock.calls.length;

    for (const endS of [101, 102, 103])
      act(() => bridge.setTime({ dataRange: { startS: 0, endS } }));
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(vi.mocked(reduceCanFrameRows).mock.calls.length).toBe(reduced);
  });
});
