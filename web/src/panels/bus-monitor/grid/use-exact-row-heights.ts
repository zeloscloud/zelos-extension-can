import type { GridApi, RowHeightParams } from "ag-grid-community";
import { useCallback, useEffect, useRef } from "react";

/** Every row's height set up front: AG Grid's guess for an unrendered row shifts the lines under the reader. */
export function useExactRowHeights<TRow>(getGridApi: () => GridApi<TRow> | null, heightOf: (row: TRow) => number) {
  // Read through a ref, so the grid holds one callback for the panel's life.
  const heightOfRef = useRef(heightOf);
  heightOfRef.current = heightOf;
  const getRowHeight = useCallback(
    (params: RowHeightParams<TRow>) => (params.data ? heightOfRef.current(params.data) : undefined),
    [],
  );

  const applyRowHeights = useCallback(() => {
    const api = getGridApi();
    if (!api) return;
    let changed = false;
    api.forEachNode((node) => {
      if (!node.data) return;
      const height = heightOf(node.data);
      if (node.rowHeight === height) return;
      node.setRowHeight(height);
      changed = true;
    });
    if (changed) api.onRowHeightChanged();
  }, [getGridApi, heightOf]);
  // A new `heightOf` (a font, a wrap width) resizes the rows already there.
  useEffect(() => applyRowHeights(), [applyRowHeights]);

  /** Spread onto the grid. */
  return { getRowHeight, onRowDataUpdated: applyRowHeights };
}
