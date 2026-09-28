/* Team widget: the pixel office of the project's workflows, shown on the
 * dashboard. Desks are steps with the model that runs them; documents are tasks.
 * It only reads state: a stack opens its task, a desk opens its step. */

import { h, memo } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { runnerOf, runs, stepOf, store, titleOf } from "../core/store.js";
import {
  mount as mountOffice,
  update as updateOffice,
} from "../graph/office.js";
import { editStep } from "./flows.js";
import { openTask } from "./shell.js";
import { roleName } from "./vocabulary.js";

/** Every workflow the project uses, most frequent first, one per shape. */
function projectFlows() {
  const counts = new Map();
  for (const run of runs())
    counts.set(run.workflow_digest, (counts.get(run.workflow_digest) || 0) + 1);
  const seen = new Set();
  const found = [];
  for (const [digest] of [...counts].sort((a, b) => b[1] - a[1])) {
    const workflow = store.state.definitions.find(
      (d) => d.digest === digest,
    )?.workflow;
    const shape = workflow?.steps.map((s) => s.id + s.kind).join();
    if (workflow && !seen.has(shape)) {
      seen.add(shape);
      found.push(workflow);
    }
  }
  return found;
}

/** Mount the office into a new element; call `update()` on store changes. */
export function teamWidget() {
  const element = h("div", { id: "office", class: "office" });
  let flows = [];
  const render = memo();
  mountOffice(element, {
    flow: () => flows,
    label: (step) => roleName(step),
    runner: (step) => runnerOf(step, t),
    summary: (tasks, people) => t("team.summary", { tasks, people }),
    inboxLabel: () => t("team.inbox"),
    shelfLabel: () => t("team.shelf"),
    docsLabel: (list) =>
      list.slice(0, 3).map(titleOf).join(" · ") +
      (list.length > 3 ? " · +" + (list.length - 3) : ""),
    emptyLabel: () => t("team.empty"),
    deskHint: (n) =>
      n ? t("team.deskBusy", { count: n }) : t("team.deskFree"),
    openRun: (run) => openTask(run.id),
    openStep: (step) => {
      const workflow =
        flows.find((f) => f.steps.some((s) => s.id === step.id)) || flows[0];
      editStep(workflow, step.id);
    },
  });
  return {
    element,
    update() {
      flows = projectFlows();
      const list = runs();
      render(
        [
          list,
          flows.map((f) => f.id + f.steps.length),
          window.ffaiPreferences.language,
        ],
        () => updateOffice(list, stepOf, flows),
      );
    },
  };
}
