/* Intake: how work enters the board and the actions over a project's sources.
 * A folded explanation, then: import drafts from the project's sources, move
 * unfinished work onto the current flow templates, and take in again everything
 * that never started. None of them starts work. */

import * as api from "../core/api.js";
import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { recall, remember } from "../core/storage.js";
import { currentProject, refresh, store } from "../core/store.js";
import { confirmDialog } from "../ui/dialog.js";
import { attempt } from "../ui/toast.js";
import { updateProjectFlows } from "../features/flow-change.js";

/** A button that posts `action` for the current project, after `confirm` when given. */
function action(id, label, message, confirm) {
  const button = h(
    "button",
    {
      type: "button",
      id,
      class: confirm ? "ghost" : "",
      onclick: async () => {
        if (confirm && !(await confirmDialog(confirm))) return;
        button.disabled = true;
        await attempt(async () => {
          const result = await api.post(id, { project: store.project });
          await refresh();
          return result;
        }, message);
        button.disabled = false;
      },
    },
    label,
  );
  return button;
}

export function intakePanel() {
  const project = currentProject();
  // Drafts are imported from a plans folder or a tracker; without either, work is typed in.
  const sourced = !!(project?.plans_folder || project?.tracker?.kind);
  return h(
    "details",
    {
      class: "intake",
      open: recall("intake-open", false),
      ontoggle: (e) => remember("intake-open", e.target.open),
    },
    h("summary", {}, t("intake.title")),
    h(
      "ol",
      { class: "intake-steps" },
      t("intake.steps")
        .split(" | ")
        .map((line) => h("li", {}, line)),
    ),
    h(
      "div",
      { class: "intake-actions" },
      sourced
        ? action("drafts-import", t("intake.import"), (r) =>
            t("intake.imported", { items: r.items, created: r.created.length }),
          )
        : h("small", { class: "hint" }, t("intake.noSources")),
      h(
        "button",
        { type: "button", id: "flows-update", class: "ghost", onclick: updateProjectFlows },
        t("flow.updateAll"),
      ),
      sourced
        ? action(
            "drafts-rebuild",
            t("intake.rebuild"),
            (r) => t("intake.rebuilt", { removed: r.removed, created: r.created.length }),
            {
              title: t("intake.rebuildTitle"),
              message: t("intake.rebuildText"),
              confirm: t("intake.rebuild"),
              danger: true,
            },
          )
        : null,
    ),
  );
}
