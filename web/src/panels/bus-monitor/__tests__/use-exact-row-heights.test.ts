import { renderHook } from "@testing-library/react";
import type { GridApi } from "ag-grid-community";
import { describe, expect, it, vi } from "vitest";
import { useExactRowHeights } from "../grid/use-exact-row-heights";

interface Row {
  id: string;
  lines: number;
}

/** The slice of a grid the hook drives: its row nodes, and the refresh after their heights change. */
function fakeGrid(rows: Row[], startHeight = 20) {
  const nodes = rows.map((data) => ({
    data,
    rowHeight: startHeight,
    setRowHeight: vi.fn(function (this: { rowHeight: number }, height: number) {
      this.rowHeight = height;
    }),
  }));
  const api = {
    forEachNode: (visit: (node: (typeof nodes)[number]) => void) => nodes.forEach(visit),
    onRowHeightChanged: vi.fn(),
  };
  return { nodes, api, getGridApi: () => api as unknown as GridApi<Row> };
}

const byLines = (row: Row) => row.lines * 20;

describe("useExactRowHeights", () => {
  // AG Grid otherwise sizes a row only when it scrolls into view, so rows above the reader jump as they size.
  it("sets every row's real height up front, rendered or not, and refreshes the grid once", () => {
    const grid = fakeGrid([
      { id: "a", lines: 1 },
      { id: "b", lines: 3 },
      { id: "c", lines: 5 },
    ]);
    const { result } = renderHook(() => useExactRowHeights(grid.getGridApi, byLines));
    result.current.onRowDataUpdated();

    expect(grid.nodes.map((node) => node.rowHeight)).toEqual([20, 60, 100]);
    expect(grid.api.onRowHeightChanged).toHaveBeenCalledTimes(1);
  });

  it("refreshes nothing when every row already has its height", () => {
    const grid = fakeGrid([{ id: "a", lines: 1 }]);
    const { result } = renderHook(() => useExactRowHeights(grid.getGridApi, byLines));
    result.current.onRowDataUpdated();

    expect(grid.nodes[0]?.setRowHeight).not.toHaveBeenCalled();
    expect(grid.api.onRowHeightChanged).not.toHaveBeenCalled();
  });

  // A latched row (CAN's, one per message id) gets new data under the same node and can grow.
  it("grows a row whose data changed under the same node", () => {
    const grid = fakeGrid([{ id: "a", lines: 1 }]);
    const { result } = renderHook(() => useExactRowHeights(grid.getGridApi, byLines));
    const node = grid.nodes[0];
    if (!node) throw new Error("no node");
    node.data = { id: "a", lines: 4 };
    result.current.onRowDataUpdated();

    expect(node.rowHeight).toBe(80);
  });

  it("resizes the rows already there when the height rule changes, keeping one callback for the grid", () => {
    const grid = fakeGrid([{ id: "a", lines: 2 }], 40);
    const { result, rerender } = renderHook(
      ({ heightOf }) => useExactRowHeights(grid.getGridApi, heightOf),
      {
        initialProps: { heightOf: byLines },
      },
    );
    const first = result.current.getRowHeight;

    rerender({ heightOf: (row: Row) => row.lines * 30 });

    expect(grid.nodes[0]?.rowHeight).toBe(60);
    expect(result.current.getRowHeight).toBe(first);
    expect(first({ data: { id: "a", lines: 2 } } as never)).toBe(60);
  });
});
