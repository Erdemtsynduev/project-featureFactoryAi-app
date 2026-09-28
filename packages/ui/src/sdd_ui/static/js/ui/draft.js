/* Drafts: form input is saved as you type and restored when the form reopens,
 * so closing a dialog, reloading the page or a polling re-render never loses
 * text. A draft is cleared only after a successful submit or an explicit reset. */

import { formValues, fillForm } from "../core/dom.js";
import { persistentMap } from "../core/storage.js";

const drafts = persistentMap("drafts", 100);

export function draftOf(key) {
  return drafts.get(key);
}

export function discardDraft(key) {
  drafts.delete(key);
}

/**
 * Bind a form to a draft slot. `onState(saved)` reports whether a draft exists.
 * Returns { restore, clear, hasDraft }.
 */
export function bindDraft(
  form,
  key,
  { onState = () => {}, exclude = [] } = {},
) {
  // Saved on every keystroke: closing or reloading right after typing loses nothing.
  const save = () => {
    const values = formValues(form);
    for (const name of exclude) delete values[name];
    drafts.set(key, { values, saved: Date.now() });
    onState(true);
  };
  form.addEventListener("input", save);
  form.addEventListener("change", save);
  return {
    restore() {
      const draft = drafts.get(key);
      if (draft) fillForm(form, draft.values);
      onState(!!draft);
      return !!draft;
    },
    clear() {
      drafts.delete(key);
      onState(false);
    },
    hasDraft: () => drafts.has(key),
  };
}
