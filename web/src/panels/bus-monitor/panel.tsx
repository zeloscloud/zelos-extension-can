import type { AppBridgePanelInfo } from "@zeloscloud/app-extension-sdk";
import { usePanel, usePanelActions, useZelosBridge } from "@zeloscloud/app-extension-sdk/react";
import type { RowHeightParams } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { useCallback, useMemo } from "react";
import { buildCanColumnDefs } from "./columns";
import { type CanRow, canRowHeight } from "./data";
import { GridPanelBody, type GridMenuRequest, PanelErrorState, PanelState, WindowStatus } from "./grid/chrome";
import { useGridPanelState, useGridRowLookup } from "./grid/use-grid-panel-state";
import { BusMonitorIcon } from "./icons";
import { canCellText, canMenuCopy, canMenuItems } from "./menu";
import { resolveBusMonitorOptions } from "./options";
import { useBusMonitorData } from "./use-bus-data";

/** The panel's message when nothing is bound yet. */
export const EMPTY_MESSAGE = "Drag a CAN Frame table or any event here";

// Module-level: AG Grid takes an overlay component by identity, so a fresh function per render would
// remount it on every poll.
const NoRowsOverlay = () => <PanelState icon={<BusMonitorIcon />} description="No CAN traffic in this time range" />;
const LoadingOverlay = () => <PanelState icon={<BusMonitorIcon />} description="Loading CAN traffic…" />;
const OVERLAYS = { noRows: NoRowsOverlay, loading: LoadingOverlay };

function BusMonitor({ panel }: { panel: AppBridgePanelInfo }) {
  const { flashChanges, showSource } = resolveBusMonitorOptions(panel.options);
  const { search, getGridApi, gridProps } = useGridPanelState<CanRow>(panel, OVERLAYS);
  const { rows, totalRows, cap, isLoading, error } = useBusMonitorData(panel);
  const actions = usePanelActions();

  const columnDefs = useMemo(() => buildCanColumnDefs({ flashChanges, showSource }), [flashChanges, showSource]);

  // A decoded message is one line per signal, so its row grows to hold them. Computed, not `autoHeight`:
  // that makes AG Grid measure every row's DOM on every refresh, and the line count already says the size.
  const rowHeight = gridProps.rowHeight;
  const getRowHeight = useCallback(
    (params: RowHeightParams<CanRow>) => (params.data ? canRowHeight(params.data, rowHeight) : rowHeight),
    [rowHeight],
  );

  // AG Grid sizes a row node ONCE, when it appears, so a latched event row that grows a field is resized
  // here. Only changed rows: `resetRowHeights()` redraws every poll, and each redraw leaks AG Grid's no-op
  // React updates.
  const onRowDataUpdated = useCallback(() => {
    const api = getGridApi();
    if (!api) return;
    let changed = false;
    api.forEachNode((node) => {
      if (!node.data) return;
      const height = canRowHeight(node.data, rowHeight);
      if (node.rowHeight === height) return;
      node.setRowHeight(height);
      changed = true;
    });
    if (changed) api.onRowHeightChanged();
  }, [getGridApi, rowHeight]);

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
        getRowHeight={getRowHeight}
        onRowDataUpdated={onRowDataUpdated}
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
  if (panel.signals.length === 0) return <PanelState icon={<BusMonitorIcon />} description={EMPTY_MESSAGE} />;
  return <BusMonitor panel={panel} />;
}
