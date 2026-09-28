/* Notification center: what needs you now, and what happened since you looked.
 *
 * "Needs you" is derived from the live state (answers and blockers in every
 * project). "Events" come from the server's flight log (questions, blocks,
 * waits, acceptances, created tickets, revivals, queue stops), so they are the
 * same for every browser and survive reloads. Unread is per browser. Optional
 * system notifications mirror new events while the page is open. */

import * as api from "../core/api.js";
import { byId, h, replace } from "../core/dom.js";
import { formatTime, t } from "../core/i18n.js";
import { recall, remember } from "../core/storage.js";
import {
  laneOf,
  projects,
  run as findRun,
  store,
  subscribe,
  titleOf,
} from "../core/store.js";
import { openDialog } from "../ui/dialog.js";
import { toast } from "../ui/toast.js";
import { openTask } from "./shell.js";
import { attentionText } from "./vocabulary.js";

const TONE = {
  question: "attention",
  blocked: "blocked",
  queue_stopped: "blocked",
  waiting: "waiting",
  revived: "waiting",
  accepted: "done",
  tickets_admitted: "done",
  bulk_resume: "working",
};
const SYSTEM = new Set(["question", "blocked", "accepted", "queue_stopped"]);
const identity = (e) => `${e.at}|${e.kind}|${e.run || ""}`;

let events = [];
let seen = recall("notifications-read", 0);
let loadedTransition;
let panel = null;
const startedAt = Date.now() / 1000;

function renderBadge() {
  const count = events.filter((e) => e.at > seen).length;
  const badge = byId("notify-badge");
  badge.hidden = !count;
  badge.textContent = count > 99 ? "99+" : String(count);
}

async function load() {
  try {
    const entries = await api.get("log", { limit: 300 });
    const fresh = entries.filter((e) => e.kind in TONE);
    const known = new Set(events.map(identity));
    for (const entry of fresh)
      if (!known.has(identity(entry)) && entry.at > startedAt) system(entry);
    events = fresh.reverse();
    renderBadge();
    if (panel) renderPanel();
  } catch {
    /* The badge keeps its last value while the server is unreachable. */
  }
}

function system(entry) {
  if (!SYSTEM.has(entry.kind) || !recall("notifications", false)) return;
  if (!("Notification" in window) || Notification.permission !== "granted")
    return;
  const task = entry.run ? findRun(entry.run) : null;
  const note = new Notification(t("log.kind." + entry.kind), {
    body: task ? titleOf(task) : entry.reason || entry.error || "",
    tag: identity(entry),
  });
  note.onclick = () => {
    window.focus();
    if (entry.run) openTask(entry.run);
    note.close();
  };
}

function goTo(id) {
  panel?.close();
  openTask(id);
}

function notice(tone, unread, body, time) {
  return h(
    "li",
    { class: `notice tone-${tone}${unread ? " unread" : ""}` },
    h("span", { class: "notice-dot", "aria-hidden": "true" }),
    h("div", { class: "notice-body" }, body),
    time,
  );
}

function eventRow(entry) {
  const task = entry.run ? findRun(entry.run) : null;
  const detail = [
    entry.reason,
    entry.error,
    entry.count !== undefined && t("log.count", { count: entry.count }),
  ]
    .filter(Boolean)
    .join(" · ");
  return notice(
    TONE[entry.kind] || "idle",
    entry.at > seen,
    [
      h("strong", {}, t("log.kind." + entry.kind)),
      entry.run
        ? h(
            "button",
            { type: "button", class: "link", onclick: () => goTo(entry.run) },
            task ? titleOf(task) : entry.run,
          )
        : null,
      detail ? h("small", { class: "notice-detail" }, detail) : null,
    ],
    h("time", {}, formatTime(entry.at)),
  );
}

function projectName(run) {
  const id = store.state.task_metadata?.[run.id]?.project;
  return projects().find((p) => p.id === id)?.name || "";
}

function systemToggle() {
  return h(
    "label",
    { class: "check-row" },
    h("input", {
      type: "checkbox",
      id: "notify-system",
      checked: recall("notifications", false),
      onchange: async (e) => {
        const box = e.target;
        if (!box.checked) return remember("notifications", false);
        if (!("Notification" in window)) {
          box.checked = false;
          return toast(t("notify.unsupported"), { tone: "error" });
        }
        const permission = await Notification.requestPermission();
        remember("notifications", permission === "granted");
        box.checked = permission === "granted";
        toast(permission === "granted" ? t("notify.on") : t("notify.denied"), {
          tone: permission === "granted" ? "success" : "error",
        });
      },
    }),
    h("span", {}, t("notify.system")),
  );
}

function renderPanel() {
  const needs = (store.state?.runs || []).filter((r) => laneOf(r) === "needs");
  replace(
    panel.body,
    h(
      "section",
      { class: "drawer-section" },
      h("h3", {}, t("notify.needs", { count: needs.length })),
      needs.length
        ? h(
            "ul",
            { class: "notices" },
            needs.map((r) =>
              notice(
                r.attention.tone,
                false,
                [
                  h(
                    "button",
                    {
                      type: "button",
                      class: "link",
                      onclick: () => goTo(r.id),
                    },
                    titleOf(r),
                  ),
                  h("small", { class: "notice-detail" }, attentionText(r)),
                ],
                h("small", { class: "hint" }, projectName(r)),
              ),
            ),
          )
        : h("p", { class: "hint" }, t("notify.nothingNeeded")),
    ),
    h(
      "section",
      { class: "drawer-section" },
      h(
        "div",
        { class: "panel-head" },
        h("h3", {}, t("notify.events")),
        h(
          "button",
          { type: "button", class: "ghost", onclick: markRead },
          t("notify.markRead"),
        ),
      ),
      events.length
        ? h("ul", { class: "notices" }, events.slice(0, 150).map(eventRow))
        : h("p", { class: "hint" }, t("log.empty")),
    ),
    systemToggle(),
  );
}

function markRead() {
  seen = Math.max(seen, events[0]?.at || 0);
  remember("notifications-read", seen);
  renderBadge();
  if (panel) renderPanel();
}

export function openNotifications() {
  if (panel) return;
  panel = openDialog({
    title: t("notify.title"),
    subtitle: t("notify.subtitle"),
    variant: "drawer",
    size: "drawer-narrow",
    body: [h("p", { class: "hint" }, t("log.loading"))],
    onClose: () => {
      panel = null;
      markRead();
    },
  });
  renderPanel();
  load();
}

export function startNotifications() {
  byId("notifications").onclick = openNotifications;
  subscribe(() => {
    const last = store.state?.last_transition;
    if (last !== loadedTransition) {
      loadedTransition = last;
      load();
    } else if (panel) renderPanel();
  });
  loadedTransition = store.state?.last_transition;
  load();
  // Entries without a transition (queue stops, bulk actions) still arrive.
  setInterval(load, 15000);
}
