/* Team: the pixel office of the project's workflows. Desks are steps with the
 * model that runs them; documents are tasks. It only reads state: clicking a
 * stack opens the task, clicking a desk opens its step in the editor. */

import { h, memo, replace } from "../core/dom.js";
import { t } from "../core/i18n.js";
import {
  hasScope,
  runnerOf,
  runs,
  stepOf,
  store,
  titleOf,
} from "../core/store.js";
import {
  mount as mountOffice,
  update as updateOffice,
} from "../graph/office.js";
import { editStep } from "./flows.js";
import { welcome } from "./onboarding.js";
import { openTask, registerView } from "./shell.js";
import { roleName } from "./vocabulary.js";

let root = null;
let office = null;
let flows = [];
const render = memo();

/** Every workflow the project uses, most frequent first. */
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

function draw() {
  if (!root) return;
  if (!hasScope()) {
    render(["welcome"], () => {
      office = null;
      replace(root, welcome());
    });
    return;
  }
  if (!office) {
    const canvas = h("div", { id: "office", class: "office" });
    replace(root, h("p", { class: "lead" }, t("team.lead")), canvas);
    mountOffice(canvas, {
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
    office = canvas;
    render(null, () => {});
  }
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
}

registerView({
  id: "team",
  order: 2,
  title: "nav.team",
  glyph: "☺",
  mount(container) {
    root = container;
    office = null;
    render(null, () => {});
    draw();
  },
  update() {
    draw();
  },
  unmount() {
    root = null;
    office = null;
  },
});
