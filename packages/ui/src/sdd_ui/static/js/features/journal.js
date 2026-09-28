/* Flight log: operator actions, queue stops, blocks, waits and revivals across
 * all tasks. Together with each task's journal it answers "what happened and
 * when" after an incident. Entries with a task open that task. */

import * as api from "../core/api.js";
import { h, replace } from "../core/dom.js";
import { formatTime, t } from "../core/i18n.js";
import { recall, remember } from "../core/storage.js";
import { run as findRun, store, titleOf } from "../core/store.js";
import { toastError } from "../ui/toast.js";
import { openTask, registerView } from "./shell.js";

let root = null;
let list = null;
let level = recall("journal-level", "");
let lastTransition = null;

function kindLabel(kind) {
  const key = "log.kind." + kind;
  const text = t(key);
  return text === key ? kind : text;
}

function describe(entry) {
  return [
    entry.action && t("log.action", { action: entry.action }),
    entry.command,
    entry.step && t("log.step", { step: entry.step }),
    entry.reason,
    entry.error,
    entry.outcome,
    entry.count !== undefined && t("log.count", { count: entry.count }),
    Array.isArray(entry.tickets) &&
      entry.tickets.length &&
      entry.tickets.join(", "),
    typeof entry.tickets === "number" &&
      entry.tickets &&
      t("log.tickets", { count: entry.tickets }),
    entry.seconds !== undefined && `${entry.seconds}s`,
  ]
    .filter(Boolean)
    .join(" · ");
}

async function load() {
  if (!list) return;
  try {
    const entries = await api.get("log", { level, limit: 400 });
    replace(
      list,
      entries.length
        ? entries
            .slice()
            .reverse()
            .map((entry) => {
              const task = entry.run ? findRun(entry.run) : null;
              return h(
                "li",
                { class: "level-" + entry.level },
                h("time", {}, formatTime(entry.at, true)),
                h("strong", {}, kindLabel(entry.kind)),
                entry.run
                  ? h(
                      "button",
                      {
                        type: "button",
                        class: "link",
                        onclick: () => openTask(entry.run),
                      },
                      task ? titleOf(task) : entry.run,
                    )
                  : h("span", {}),
                h("span", { class: "detail" }, describe(entry)),
              );
            })
        : h("li", { class: "empty" }, t("log.empty")),
    );
  } catch (error) {
    toastError(error);
  }
}

function build() {
  const filter = h(
    "div",
    { class: "segmented", role: "group", "aria-label": t("log.level") },
    ["", "warning", "error"].map((value) =>
      h(
        "button",
        {
          type: "button",
          class: level === value ? "active" : "",
          "aria-pressed": String(level === value),
          onclick: () => {
            level = value;
            remember("journal-level", value);
            mount(root);
          },
        },
        t("log.level." + (value || "all")),
      ),
    ),
  );
  list = h("ol", { class: "timeline journal" });
  return [
    h(
      "div",
      { class: "view-intro" },
      h("p", { class: "lead" }, t("log.lead")),
      h(
        "div",
        { class: "toolbar" },
        filter,
        h("button", { type: "button", onclick: load }, t("action.refresh")),
      ),
    ),
    list,
  ];
}

function mount(container) {
  root = container;
  replace(root, build());
  load();
}

registerView({
  id: "journal",
  order: 6,
  title: "nav.journal",
  glyph: "☰",
  mount,
  update(_, reason) {
    if (reason === "language") return mount(root);
    // New transitions usually mean new entries; reload only then.
    const last = store.state?.last_transition;
    if (last !== lastTransition) {
      lastTransition = last;
      load();
    }
  },
  unmount() {
    root = null;
    list = null;
  },
});
