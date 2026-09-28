/* Pieces every drawer tab uses. */

import { h } from "../core/dom.js";
import { titleOf } from "../core/store.js";
import { openTask } from "../features/shell.js";
import { statusPill } from "../features/vocabulary.js";

export function section(title, ...children) {
  return h(
    "section",
    { class: "drawer-section" },
    title ? h("h3", {}, title) : null,
    children,
  );
}

export function safeJson(text) {
  try {
    return JSON.parse(text || "{}");
  } catch {
    return {};
  }
}

/** Linked task titles with their state pills. */
export function taskLinks(list) {
  return h(
    "ul",
    { class: "child-list" },
    list.map(({ id, run }) =>
      h(
        "li",
        {},
        h(
          "button",
          { type: "button", class: "link", onclick: () => openTask(id) },
          run ? titleOf(run) : id,
        ),
        " ",
        run ? statusPill(run) : null,
      ),
    ),
  );
}
