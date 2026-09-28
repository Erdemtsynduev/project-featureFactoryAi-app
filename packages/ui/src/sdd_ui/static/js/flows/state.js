/* Workflow editor state shared by its parts: the draft, selection, zoom, mode
 * and the editor's elements. */

import { formatTime, t } from "../core/i18n.js";
import { recall, remember } from "../core/storage.js";

export const KINDS = ["agent", "check", "human", "condition", "operation", "finish"];

export const editor = {
  flow: null,
  selected: 0,
  zoom: 1,
  connecting: null,
  selectedEdge: null,
  size: { width: 1, height: 1 },
  saved: null, // time of the last local save
  dirty: false, // edited since loaded or published
  editing: recall("flow-editing", false),
};
// Editor elements by role; filled by each build, never replaced.
export const nodes = {};

/* Draft persistence: every edit is saved in this browser at once. */

export function persist() {
  editor.saved = Date.now() / 1000;
  editor.dirty = true;
  remember("flow-draft", { flow: editor.flow, saved: editor.saved });
  showSaved();
}

export function showSaved() {
  if (nodes.saved)
    nodes.saved.textContent = editor.saved
      ? t("flows.savedAt", { time: formatTime(editor.saved) })
      : "";
}
