/* Details tab and the dependency panel: facts, parent and children, waits, the
 * feature's documents, memory, brief, lane, instruction and raw results. */

import { h } from "../core/dom.js";
import { money, t } from "../core/i18n.js";
import {
  childrenOf,
  dependentsOf,
  kindOf,
  outsidePrerequisites,
  prerequisitesOf,
  run as findRun,
  runnerOf,
  store,
  titleOf,
} from "../core/store.js";
import { openBulkResume } from "../features/bulk.js";
import { flowSection } from "../features/flow-change.js";
import { openTask } from "../features/shell.js";
import {
  attentionText,
  kindLabel,
  statusPill,
  stepName,
} from "../features/vocabulary.js";
import {
  closable,
  closeButton,
  resumeChildrenButton,
} from "../features/work.js";
import { safeJson, section, taskLinks } from "./parts.js";

/** What this task waits for, right under the banner, with one way to unblock it. */
export function waitingPanel(run) {
  const pending = run.pending_dependencies || [];
  if (!pending.length || run.status === "accepted") return null;
  const chain = outsidePrerequisites([run]);
  const startable = [run, ...chain].filter(
    (r) => r.paused && r.status !== "blocked" && !r.active,
  );
  return h(
    "section",
    { class: "waiting-panel" },
    h(
      "div",
      { class: "panel-head" },
      h("h3", {}, t("detail.deps.pending", { count: pending.length })),
      startable.length > 1 || (startable.length && startable[0] !== run)
        ? h(
            "button",
            {
              type: "button",
              class: "primary-soft",
              onclick: () =>
                openBulkResume("", { ids: [run.id], title: titleOf(run) }),
            },
            t("detail.deps.start"),
          )
        : null,
    ),
    taskLinks(pending.map((id) => ({ id, run: findRun(id) }))),
  );
}

/** A feature's specification (PRD) and ticket breakdown, as the factory keeps them. */
function documentsSection(detail) {
  const docs = detail.documents || {};
  if (!docs.specification && !docs.tickets?.length) return null;
  const download = () => {
    const blob = new Blob([docs.specification], { type: "text/markdown" });
    const link = h("a", {
      href: URL.createObjectURL(blob),
      download: `${detail.run.id}-spec.md`,
    });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  };
  return section(
    t("detail.documents"),
    docs.specification
      ? h(
          "details",
          { class: "spec-doc", open: true },
          h("summary", {}, t("detail.specification")),
          h("pre", { class: "brief" }, docs.specification),
          h(
            "div",
            { class: "form-actions" },
            h("button", { type: "button", onclick: download }, t("detail.downloadSpec")),
          ),
        )
      : null,
    docs.tickets?.length
      ? h(
          "details",
          { class: "spec-doc", open: true },
          h("summary", {}, t("detail.breakdown", { count: docs.tickets.length })),
          h(
            "ol",
            { class: "ticket-list" },
            docs.tickets.map((ticket) =>
              h(
                "li",
                {},
                h("strong", {}, `${ticket.id} · ${ticket.title}`),
                ticket.depends_on?.length
                  ? h("small", { class: "hint" }, " ← " + ticket.depends_on.join(", "))
                  : null,
                ticket.goal ? h("p", {}, ticket.goal) : null,
                ticket.acceptance?.length
                  ? h("ul", {}, ticket.acceptance.map((a) => h("li", {}, a)))
                  : null,
              ),
            ),
          ),
        )
      : null,
    docs.folder ? h("p", { class: "hint mono" }, t("detail.artifacts", { path: docs.folder })) : null,
  );
}

function dependencySections(run) {
  const waits = prerequisitesOf(run);
  const blocks = dependentsOf(run).map((r) => ({ id: r.id, run: r }));
  return [
    waits.length ? section(t("detail.deps.waits"), taskLinks(waits)) : null,
    blocks.length ? section(t("detail.deps.blocks"), taskLinks(blocks)) : null,
  ];
}

function memoryNotes(detail) {
  const seen = new Set();
  for (const result of [...detail.results].reverse())
    for (const note of safeJson(result.data).notes || []) seen.add(note);
  return [...seen];
}

export function details(detail, step) {
  const run = detail.run;
  const m = detail.metadata || {};
  const children = childrenOf(run);
  const notes = memoryNotes(detail);
  const facts = [
    [t("detail.kind"), kindLabel(kindOf(run))],
    [
      t("detail.flow"),
      detail.workflow.id + " · " + run.workflow_digest.slice(0, 10),
    ],
    [t("detail.step"), `${stepName(step || run.step)} · ${runnerOf(step, t)}`],
    [
      t("detail.calls"),
      `${run.calls} (${t("detail.planning")}: ${run.planning_calls})`,
    ],
    [
      t("detail.tokensLabel"),
      run.tokens.toLocaleString() + (run.usage_unknown ? " + ?" : ""),
    ],
    [t("detail.cost"), money(store.state.usage?.per_run_usd?.[run.id])],
    [t("detail.workspace"), store.state.locations?.[run.id] || "—"],
  ];
  return [
    section(
      "",
      h(
        "dl",
        { class: "facts" },
        facts.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]),
      ),
    ),
    m.parent
      ? section(
          t("detail.parent"),
          // Imported tickets may name a feature that was never imported.
          findRun(m.parent)
            ? h(
                "button",
                {
                  type: "button",
                  class: "link",
                  onclick: () => openTask(m.parent),
                },
                titleOf(findRun(m.parent)),
              )
            : h("p", { class: "mono" }, m.parent),
        )
      : null,
    m.origin
      ? section(
          t("detail.origin"),
          findRun(m.origin)
            ? h(
                "button",
                {
                  type: "button",
                  class: "link",
                  onclick: () => openTask(m.origin),
                },
                titleOf(findRun(m.origin)),
              )
            : h("p", { class: "mono" }, m.origin),
        )
      : null,
    children.length
      ? section(
          t("detail.children", { count: children.length }),
          run.progress
            ? h(
                "div",
                { class: "delivery" },
                h("p", {}, attentionText(run)),
                h(
                  "div",
                  { class: "button-row" },
                  run.attention?.action === "resume_children"
                    ? resumeChildrenButton(run)
                    : null,
                  closable(run) ? closeButton(run) : null,
                ),
              )
            : null,
          h(
            "ul",
            { class: "child-list" },
            children.map((child) =>
              h(
                "li",
                {},
                h(
                  "button",
                  {
                    type: "button",
                    class: "link",
                    onclick: () => openTask(child.id),
                  },
                  titleOf(child),
                ),
                " ",
                statusPill(child),
              ),
            ),
          ),
        )
      : null,
    ...dependencySections(run),
    flowSection(detail),
    documentsSection(detail),
    section(
      t("detail.memory"),
      notes.length
        ? h(
            "ul",
            { class: "notes" },
            notes.map((n) => h("li", {}, n)),
          )
        : h("p", { class: "hint" }, t("detail.memoryEmpty")),
    ),
    section(t("detail.brief"), h("pre", { class: "brief" }, detail.context)),
    detail.lane?.root ? laneInfo(detail.lane) : null,
    step?.prompt
      ? h(
          "details",
          {},
          h("summary", {}, t("detail.prompt")),
          h("pre", {}, step.prompt),
        )
      : null,
    h(
      "details",
      {},
      h("summary", {}, t("detail.raw")),
      h("pre", {}, JSON.stringify(detail.results, null, 2)),
    ),
  ];
}

function laneInfo(lane) {
  return section(
    lane.status === "removed" ? t("lane.merged") : t("lane.isolated"),
    h("p", { class: "mono" }, lane.root),
    h(
      "ul",
      {},
      (lane.repos || []).map((repo) =>
        h(
          "li",
          {},
          h("code", {}, repo.path),
          " ",
          repo.fresh
            ? t("lane.fresh")
            : t("lane.from", {
                origin: repo.origin || "HEAD",
                base: (repo.base || "").slice(0, 10),
              }),
          repo.branch ? h("code", {}, " " + repo.branch) : null,
        ),
      ),
    ),
  );
}
