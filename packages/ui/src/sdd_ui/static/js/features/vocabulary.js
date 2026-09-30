/* Names for steps, roles and task states, plus the one-line "why" of a task.
 * Every view uses these helpers so a state reads the same everywhere. */

import { h } from "../core/dom.js";
import { formatTime, relativeTime, t } from "../core/i18n.js";
import { run as runById, titleOf } from "../core/store.js";

const KNOWN_STEPS = new Set([
  "spec",
  "tickets",
  "implement",
  "checks",
  "review",
  "diagnose",
  "repair",
  "reconcile",
  "replan",
  "decide",
  "interview",
  "approve",
  "accepted",
  "finish",
  "ask",
  "answer",
  "integrate",
  "rebase",
  "resolve",
  "merge_conflict",
]);

export function stepName(step) {
  const id = typeof step === "string" ? step : step?.id;
  if (!id) return "";
  if (KNOWN_STEPS.has(id)) return t("step." + id);
  if (/^check_\d+$/.test(id)) return t("step.check", { n: id.slice(6) });
  try {
    const title = JSON.parse(step?.config || "{}").title;
    if (title) return title;
  } catch {
    /* A malformed config only loses the friendly title. */
  }
  return id;
}

export function roleName(step) {
  if (step.kind === "human") return t("role.you");
  if (step.kind === "check") return t("role.tester");
  if (step.handler?.startsWith("lane-")) return t("role.integrator");
  const key = "role." + step.id;
  const role = t(key);
  return role === key ? stepName(step) : role;
}

export function kindLabel(kind) {
  return t("kind." + kind);
}

export const KIND_GLYPH = { feature: "◇", ticket: "▣", task: "○" };

/** The server's attention reason as text, e.g. "Paused — press Resume". */
export function attentionText(run) {
  const a = run.attention || {};
  const params = {
    detail: a.detail || "",
    until: a.until ? formatTime(a.until) : "",
  };
  if (a.code === "waiting" && a.until) params.in = relativeTime(a.until);
  if (a.code === "dependencies")
    params.detail = namesOf(run.pending_dependencies || []);
  if (a.code === "paths_held") params.detail = namesOf(params.detail.split(", "));
  return t("attention." + (a.code || "queued"), params);
}

/** "ASM-12 — …, ASM-15 — … и ещё 2": titles instead of raw ids. */
function namesOf(ids) {
  const names = ids.slice(0, 2).map((id) => {
    const title = titleOf({ id });
    return title.length > 40 ? title.slice(0, 39) + "…" : title;
  });
  const more =
    ids.length > 2
      ? " " + t("attention.dependenciesMore", { count: ids.length - 2 })
      : "";
  return names.join(", ") + more;
}

/** A ticket's place in its parent's plan: "T15 · wave 3", a HITL mark and, while
 * it still waits, which tickets must be accepted first. Null for other work. */
export function ticketPlace(run) {
  const place = run.ticket;
  if (!place) return null;
  const waiting = (run.pending_dependencies || []).map(
    (id) => runById(id)?.ticket?.key || id,
  );
  return h(
    "span",
    { class: "ticket-place" },
    h(
      "span",
      { class: "ticket-chip", title: t("ticket.waveHint", { wave: place.wave }) },
      t("ticket.place", { key: place.key, wave: place.wave }),
    ),
    ...needChips(place.needs || (place.hitl ? ["human"] : [])),
    waiting.length && run.status !== "accepted"
      ? h(
          "span",
          { class: "ticket-after", title: waiting.join(", ") },
          t("ticket.after", {
            keys:
              waiting.slice(0, 4).join(", ") +
              (waiting.length > 4 ? " +" + (waiting.length - 4) : ""),
          }),
        )
      : null,
  );
}

/** Tickets of one parent in plan order: by wave, then by their number. */
export function planOrder(a, b) {
  const pa = a.ticket;
  const pb = b.ticket;
  if (!pa || !pb) return 0;
  const number = (key) => parseInt(String(key).replace(/\D+/g, ""), 10) || 0;
  return pa.wave - pb.wave || number(pa.key) - number(pb.key);
}

export function attentionTone(run) {
  return run.attention?.tone || "idle";
}

/** A status pill with the attention tone. */
export function statusPill(run) {
  return h(
    "span",
    { class: "pill tone-" + attentionTone(run) },
    t("tone." + attentionTone(run)),
  );
}

/** "4 мин 12 с" since a moment in seconds; updated in place by the ticker. */
export function elapsed(since, now = Date.now() / 1000) {
  const total = Math.max(0, Math.round(now - since));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return t("time.hm", { h: hours, m: minutes });
  if (minutes) return t("time.ms", { m: minutes, s: seconds });
  return t("time.s", { s: seconds });
}

/** Every element with data-since shows a live elapsed time. */
export function startTicker() {
  setInterval(() => {
    for (const node of document.querySelectorAll("[data-since]"))
      node.textContent = elapsed(Number(node.dataset.since));
    for (const node of document.querySelectorAll("[data-ago]"))
      node.textContent = t("live.ago", {
        time: elapsed(Number(node.dataset.ago)),
      });
  }, 1000);
}

/** What a ticket needs besides its agent: a person, an asset, the internet. */
export function needChips(needs) {
  return (needs || []).map((need) =>
    h("span", { class: "ticket-hitl", title: t("needs.hint." + need) }, t("needs." + need)),
  );
}
