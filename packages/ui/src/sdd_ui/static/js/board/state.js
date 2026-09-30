/* Board state shared by its parts: filters, folded tree rows, column paging and
 * the mounted view (its root element and how to redraw it). */

import { recall } from "../core/storage.js";

export const LANES = ["queue", "running", "needs", "done"];
export const MODES = ["tree", "live"];
export const PAGE = 40;

const mode = recall("board-mode", "tree");
export const filters = {
  mode: MODES.includes(mode) ? mode : "tree",
  // One lane to show in the tree ("" shows every lane).
  focus: recall("board-focus", ""),
  hideDone: recall("board-hide-done", false),
  query: recall("task-search", ""),
  label: recall("label-filter", ""),
};
/** Tree rows the operator folded or unfolded, by run id: true is open. */
export const folds = new Map(Object.entries(recall("tree-folds", {})));
/** Cards shown per column (key: lane); reset when filters change. */
export const shown = new Map();

export const view = { root: null, redraw: () => {} };
