import type {
  AppBridgePanelSignal,
  ColumnMetadata,
  LatestSignalValue,
  MockBridge,
  QueryCellValue,
} from "@zeloscloud/app-extension-sdk";

/**
 * A synthetic bus for `npm run dev`, where the SDK's mock host stands in for Zelos: one frame table with
 * a counter, a constant and an extended id, and one decoded message.
 */

const FRAME = { source: "CAN", message: "can0/Frame", eventType: "zelos.can.frame.v1" };
const STATUS = { source: "CAN", message: "can0/0064_DUT_Status", eventType: "zelos.can.message.v1" };

function signal(table: typeof FRAME, name: string, unit: string | null = null): AppBridgePanelSignal {
  return { ...table, signal: name, color: "", path: `${table.source}/${table.message}.${name}`, unit };
}

export const DEV_SIGNALS: AppBridgePanelSignal[] = [
  ...["arbitration_id", "is_extended", "dlc", "data", "is_fd"].map((name) => signal(FRAME, name)),
  signal(STATUS, "state"),
  signal(STATUS, "soc", "%"),
];

const column = (name: string): ColumnMetadata => ({
  source: name === "" ? "time_s" : FRAME.source,
  message: name === "" ? "" : FRAME.message,
  signal: name,
  producer: "dev-agent",
  tracePath: null,
  dataSegmentId: null,
  startTimeS: null,
  endTimeS: null,
});

const hex = (bytes: number[]) => `0x${bytes.map((byte) => byte.toString(16).padStart(2, "0")).join("")}`;

/** Push a frame window and the decoded values every 200 ms. Returns the stop function. */
export function startDevFeed(bridge: MockBridge): () => void {
  let tick = 0;
  const frames = () => {
    tick += 1;
    const nowS = Date.now() / 1000;
    const rows: QueryCellValue[][] = [[], [], [], [], []];
    const push = (timeS: number, id: number, extended: boolean, bytes: number[]) => {
      for (const [i, cell] of [timeS, id, extended, bytes.length, hex(bytes)].entries()) rows[i]?.push(cell);
    };
    for (let back = 0; back < 10; back++) {
      const t = tick - back;
      push(nowS - back * 0.2, 0x064, false, [t & 0xff, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, (0xff - t) & 0xff]);
      push(nowS - back * 0.2 - 0.05, 0x12c, false, [0xde, 0xad]);
      push(nowS - back * 0.2 - 0.1, 0x1ffffff0, true, [t & 0x0f, 0, 0, 1]);
    }
    return {
      dataset: {
        columns: ["", "arbitration_id", "is_extended", "dlc", "data"].map(column),
        data: rows,
        range: null,
        queryDurationS: 0,
      },
      latest: null,
      timeMode: "absolute",
      totalRows: rows[0]?.length ?? 0,
      isLoading: false,
      error: null,
    };
  };
  const latest = () => {
    const timeNs = `${BigInt(Date.now()) * 1_000_000n}`;
    const value = (name: string, text: string): LatestSignalValue => ({
      ...STATUS,
      signal: name,
      value: text,
      producer: "dev-agent",
      tracePath: null,
      timeNs,
      dataSegmentId: null,
    });
    return {
      dataset: null,
      latest: [value("state", `${Math.floor(tick / 25) % 4}`), value("soc", (80 - tick * 0.01).toFixed(2))],
      timeMode: "absolute",
      totalRows: 0,
      isLoading: false,
      error: null,
    };
  };
  const stops = [bridge.startPush("frames", frames, 200), bridge.startPush("latest", latest, 200)];
  return () => {
    for (const stop of stops) stop();
  };
}
