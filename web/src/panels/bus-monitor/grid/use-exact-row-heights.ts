import type { GridApi, RowHeightParams } from "ag-grid-community";
import { useCallback, useEffect, useRef } from "react";

/**
 * Every row's height set before the row renders, for a grid whose rows differ in height.
 *
 * AG Grid sizes a `getRowHeight` row only once it scrolls into view, guessing the rest at one row's height.
 * Each guess it corrects above the viewport shifts every line under the reader, and the tail chase stops
 * short of rows it hasn't sized. So on every data update the grid gets each row's real height up front.
 *
 * `heightOf` must be cheap and pure: it runs for every row on every update.
 */
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
