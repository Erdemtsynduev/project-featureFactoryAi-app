/* Changing the flow of work in progress: skip a step, run a step with another agent
 * profile, or move the task onto the current version of its template. The engine
 * publishes a new flow version and migrates the task; started work keeps its
 * progress where steps are unchanged. Each change is a request to the server; the
 * rules live there. */

import * as api from "../core/api.js";
import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { profileNames, refresh, store } from "../core/store.js";
import { confirmDialog } from "../ui/dialog.js";
import { attempt } from "../ui/toast.js";
import { stepName } from "./vocabulary.js";

async function change(run, changes) {
  return attempt(async () => {
    await api.post("flow-change", { id: run.id, version: run.version, changes });
    await refresh();
  }, t("flow.changed"));
}

async function skip(run, step) {
  if (step.required) {
    const sure = await confirmDialog({
      title: t("flow.skipRequiredTitle", { step: stepName(step) }),
      message: t("flow.skipRequiredText"),
      confirm: t("flow.skip"),
      danger: true,
    });
    if (!sure) return;
  }
  await change(run, [{ kind: "skip", step: step.id, required: !!step.required }]);
}

function profileSelect(run, step) {
  const current = step.profile !== "default" ? step.profile : step.handler;
  const names = [...new Set([current, ...profileNames()])];
  return h(
    "select",
    {
      "aria-label": t("flow.profile", { step: stepName(step) }),
      onchange: (event) =>
        change(run, [{ kind: "profile", step: step.id, profile: event.target.value }]),
    },
    names.map((name) => h("option", { value: name, selected: name === current }, name)),
  );
}

/** The drawer section; nothing for finished work or while an attempt runs. */
export function flowSection(detail) {
  const run = detail.run;
  if (run.status === "accepted") return null;
  const steps = detail.workflow.steps.filter((step) => step.kind !== "finish");
  const busy = !!run.active;
  return h(
    "section",
    { class: "drawer-section" },
    h("h3", {}, t("flow.section")),
    busy ? h("p", { class: "hint" }, t("flow.busy")) : null,
    h(
      "ul",
      { class: "flow-steps" },
      steps.map((step) =>
        h(
          "li",
          { class: step.id === run.step ? "current" : "" },
          h("span", {}, stepName(step) + (step.required ? " *" : "")),
          step.kind === "agent" && !busy ? profileSelect(run, step) : null,
          busy
            ? null
            : h(
                "button",
                { type: "button", class: "ghost", onclick: () => skip(run, step) },
                t("flow.skip"),
              ),
        ),
      ),
    ),
    busy
      ? null
      : h(
          "button",
          {
            type: "button",
            onclick: () =>
              attempt(async () => {
                await api.post("flow-update", { id: run.id, version: run.version });
                await refresh();
              }, t("flow.updated")),
          },
          t("flow.update"),
        ),
  );
}

/** Board intake: move the project's unfinished idle work onto current templates. */
export async function updateProjectFlows() {
  const project = store.project;
  const report = await attempt(() => api.post("flows-update", { project, dry: true }));
  if (!report) return;
  if (!report.outdated.length) {
    await attempt(async () => null, t("flow.noneOutdated"));
    return;
  }
  const sure = await confirmDialog({
    title: t("flow.updateAllTitle"),
    message: t("flow.updateAllText", { count: report.outdated.length }),
    confirm: t("flow.updateAll"),
  });
  if (!sure) return;
  await attempt(
    async () => {
      const result = await api.post("flows-update", { project });
      await refresh();
      return result;
    },
    (result) =>
      t("flow.updatedAll", {
        updated: result.updated.length,
        refused: Object.keys(result.refused).length,
      }),
  );
}
