/* First run and help. Without a project there is no board and no team: the
 * welcome screen walks through agents → project → first task, reading progress
 * from real state. Help explains how work flows, in the operator's words. */

import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { profileNames, projects } from "../core/store.js";
import { openDialog } from "../ui/dialog.js";
import { openProjectDialog } from "./projects.js";
import { go } from "./shell.js";

function stepItem(index, key, done, action) {
  return h(
    "li",
    { class: done ? "done" : "" },
    h(
      "span",
      { class: "step-mark", "aria-hidden": "true" },
      done ? "✓" : index,
    ),
    h(
      "div",
      {},
      h("strong", {}, t(`welcome.${key}.title`)),
      h("p", {}, t(`welcome.${key}.text`)),
      done ? null : action,
    ),
  );
}

export function welcome() {
  const agents = profileNames().length > 0;
  const project = projects().length > 0;
  return h(
    "section",
    { class: "welcome" },
    h("h2", {}, t("welcome.title")),
    h("p", { class: "lead" }, t("welcome.lead")),
    h(
      "ol",
      { class: "welcome-steps" },
      stepItem(
        1,
        "agents",
        agents,
        h(
          "button",
          { type: "button", class: "primary", onclick: () => go("agents") },
          t("welcome.agents.action"),
        ),
      ),
      stepItem(
        2,
        "project",
        project,
        h(
          "button",
          {
            type: "button",
            class: agents ? "primary" : "",
            onclick: () => openProjectDialog(),
          },
          t("project.add"),
        ),
      ),
      stepItem(
        3,
        "task",
        false,
        h("p", { class: "hint" }, t("welcome.task.after")),
      ),
    ),
    h(
      "p",
      { class: "hint" },
      t("welcome.demoHint"),
      " ",
      h(
        "button",
        {
          type: "button",
          class: "link",
          onclick: () => document.dispatchEvent(new Event("ffai-demo")),
        },
        t("welcome.demo"),
      ),
    ),
  );
}

const HELP_SECTIONS = [
  "flows",
  "queue",
  "answers",
  "stuck",
  "limits",
  "usage",
  "memory",
  "log",
];

export function openHelp() {
  openDialog({
    title: t("help.title"),
    size: "wide",
    body: HELP_SECTIONS.map((key) =>
      h(
        "section",
        { class: "help-section" },
        h("h3", {}, t(`help.${key}.title`)),
        h("p", {}, t(`help.${key}.text`)),
      ),
    ),
  });
}

export function startOnboarding() {
  document.getElementById("help").onclick = openHelp;
}
