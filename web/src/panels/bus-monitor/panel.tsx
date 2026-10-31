import type { AppBridgePanelInfo } from "@zeloscloud/app-extension-sdk";
import {
  usePanel,
  usePanelActions,
  useTimeState,
  useWorkspace,
  useZelosBridge,
} from "@zeloscloud/app-extension-sdk/react";
import { AgGridReact } from "ag-grid-react";
import { useCallback, useMemo } from "react";
import { buildCanColumnDefs, canColumnWidths } from "./columns";
import { type CanRow, canRowHeight } from "./data";
import {
  GridPanelBody,
  type GridMenuRequest,
  PanelErrorState,
  PanelState,
  WindowStatus,
} from "./grid/chrome";
import { useExactRowHeights } from "./grid/use-exact-row-heights";
import { useGridPanelState, useGridRowLookup } from "./grid/use-grid-panel-state";
import { BusMonitorIcon } from "./icons";
import { canCellText, canMenuCopy, canMenuItems } from "./menu";
import { resolveBusMonitorOptions, resolveFontSizePx } from "./options";
import { toTimeMode } from "./time";
import { useBusMonitorData } from "./use-bus-data";

/** The panel's message when nothing is bound yet. */
export const EMPTY_MESSAGE = "Drag CAN frames or messages onto this panel to view them.";

// Module-level: AG Grid takes an overlay by identity, so a fresh function per render would remount it.
const NoRowsOverlay = () => (
  <PanelState icon={<BusMonitorIcon />} description="No CAN traffic in this time range" />
);
const LoadingOverlay = () => (
  <PanelState icon={<BusMonitorIcon />} description="Loading CAN traffic…" />
);
const OVERLAYS = { noRows: NoRowsOverlay, loading: LoadingOverlay };

function BusMonitor({ panel }: { panel: AppBridgePanelInfo }) {
  const { flashChanges, showSource } = resolveBusMonitorOptions(panel.options);
  const workspace = useWorkspace();
  // One saved layout seeds many workspaces with the same panel ids, so session state is kept per workspace.
  const stateKey = `${workspace?.id ?? ""}/${panel.instanceId}`;
  const { search, getGridApi, gridProps } = useGridPanelState<CanRow>(panel, stateKey, OVERLAYS);
  const { rows, totalRows, cap, isLoading, error } = useBusMonitorData(panel, stateKey);
  const actions = usePanelActions();

  const time = useTimeState();
  const timeMode = toTimeMode(time?.mode);
  const fontSize = resolveFontSizePx(panel.options?.fontSize);
  // Only an elapsed time's width follows the data's ends.
  const startS = timeMode === "relative" ? (time?.dataRange?.startS ?? 0) : 0;
  const endS = timeMode === "relative" ? (time?.dataRange?.endS ?? 0) : 0;
  const widths = useMemo(
    () => canColumnWidths(timeMode, fontSize, startS, endS),
    [timeMode, fontSize, startS, endS],
  );
  const columnDefs = useMemo(
    () => buildCanColumnDefs({ flashChanges, showSource, widths }),
    [flashChanges, showSource, widths],
  );

  // Computed, not `autoHeight`, which measures every row's DOM on every refresh: the line count says the size.
  const rowHeight = gridProps.rowHeight;
  const heightOf = useCallback(
    (row: CanRow) => canRowHeight(row, rowHeight, fontSize),
    [rowHeight, fontSize],
  );
  const exactHeights = useExactRowHeights(getGridApi, heightOf);

  // The menu works from a SNAPSHOT of the row, so it outlives the row being replaced by a newer frame.
  const lookupRow = useGridRowLookup(rows);
  const onCellMenu = useCallback(
    async ({ cell, x, y }: GridMenuRequest) => {
      const row = lookupRow(cell.rowId);
      if (!row) return;
      const target = { row, cellText: canCellText(row, cell.colId, cell.text) };
      try {
        const choice = await actions.showMenu({ x, y, items: canMenuItems(target) });
        const copy = canMenuCopy(choice, target);
        if (copy) await actions.copyText(copy.text, copy.toast);
      } catch (cause) {
        console.warn("[bus-monitor] context menu failed", cause);
      }
    },
    [lookupRow, actions],
  );

  return (
    <GridPanelBody
      error={error}
      search={search}
      onCellMenu={onCellMenu}
      footer={<WindowStatus shown={cap} total={totalRows} />}
    >
      <AgGridReact<CanRow>
        {...gridProps}
        rowData={rows}
        columnDefs={columnDefs}
        {...exactHeights}
        loading={isLoading}
      />
    </GridPanelBody>
  );
}

/** The CAN Bus Monitor: one latched row per arbitration id or decoded message, updated in place. */
export function BusMonitorPanel() {
  const { status, error } = useZelosBridge();
  const panel = usePanel();

  if (status === "error") return <PanelErrorState message={error.message} />;
  if (!panel) return <PanelState description="Connecting to Zelos…" />;
  if (panel.signals.length === 0)
    return <PanelState icon={<BusMonitorIcon />} description={EMPTY_MESSAGE} />;
  return <BusMonitor panel={panel} />;
}
