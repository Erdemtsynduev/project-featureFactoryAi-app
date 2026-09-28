/* Board state shared by its parts: filters, open plan rows, column paging and the
 * mounted view (its root element and how to redraw it). */

import { recall } from "../core/storage.js";

export const LANES = ["queue", "running", "needs", "done"];
export const PAGE = 40;
export const NO_PLAN = "";

export const filters = {
  mode: recall("board-mode", "live"),
  kind: recall("kind-filter", ""),
  query: recall("task-search", ""),
  plan: recall("plan-filter", ""),
};
export const openPlans = new Set(recall("open-plans", []));
/** Cards shown per column (key: lane, or plan + lane); reset when filters change. */
export const shown = new Map();

export const view = { root: null, redraw: () => {} };
