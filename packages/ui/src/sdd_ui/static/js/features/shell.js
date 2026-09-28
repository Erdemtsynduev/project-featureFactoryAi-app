/* Application frame: routing, navigation, project switcher, queue control and
 * meters. Views register themselves; the shell mounts one at a time and passes
 * every store change to it. Routes are hash tokens: #board, #task/<id>, ... */

import { byId, h, replace, stableLabel } from "../core/dom.js";
import { toggleQueue } from "./bulk.js";
import { applyStatic, formatNumber, money, t } from "../core/i18n.js";
import * as api from "../core/api.js";
import {
  currentProject,
  hasScope,
  looseRuns,
  LOOSE,
  projects,
  runs,
  selectProject,
  store,
  subscribe,
  refresh,
  stopPolling,
  laneOf,
} from "../core/store.js";
import { attempt, toast } from "../ui/toast.js";
import { confirmDialog } from "../ui/dialog.js";

const views = new Map();
let current = null;
let depth = 0;
const taskListeners = new Set();

export function registerView(view) {
  views.set(view.id, view);
}

/** Views may react to task routes (the drawer opens over the current view). */
export function onTaskRoute(listener) {
  taskListeners.add(listener);
}

/* History entries carry their depth, so Back is enabled only inside the app. */
export function go(route) {
  if (location.hash === "#" + route) return;
  depth++;
  history.pushState({ depth }, "", "#" + route);
  routeNow();
}

export function openTask(id) {
  go("task/" + encodeURIComponent(id));
}

/** Leave a task route without adding history: back if we came from inside the app. */
export function closeTaskRoute() {
  if (!location.hash.startsWith("#task/")) return;
  if (depth > 0) history.back();
  else {
    history.replaceState({ depth }, "", "#" + (current?.id || "board"));
    routeNow();
  }
}

function parse() {
  const hash = decodeURIComponent(location.hash.slice(1));
  if (hash.startsWith("task/")) return { view: null, task: hash.slice(5) };
  return { view: views.has(hash) ? hash : "overview", task: null };
}

function routeNow() {
  const target = parse();
  const view = views.get(target.view || current?.id || "overview");
  if (view !== current) mount(view);
  for (const listener of taskListeners) listener(target.task);
  byId("nav-back").disabled = depth <= 0;
}

function mount(view) {
  current?.unmount?.();
  current = view;
  const container = byId("view");
  container.replaceChildren();
  container.className = "view view-" + view.id;
  view.mount(container);
  view.update?.(store, "mount");
  renderNav();
  renderTitle();
  container.focus?.();
}

function renderTitle() {
  byId("page-title").textContent = current ? t(current.title) : "";
  document.title =
    (current ? t(current.title) + " · " : "") + "Feature Factory AI";
}

function renderNav() {
  const nav = byId("nav");
  const needs = runs().filter((r) => laneOf(r) === "needs").length;
  replace(
    nav,
    [...views.values()]
      .sort((a, b) => a.order - b.order)
      .map((view) =>
        h(
          "button",
          {
            type: "button",
            class: view === current ? "active" : "",
            "aria-current": view === current ? "page" : false,
            onclick: () => go(view.id),
          },
          h("span", { class: "nav-glyph", "aria-hidden": "true" }, view.glyph),
          h("span", { class: "nav-label" }, t(view.title)),
          view.id === "board" && needs
            ? h(
                "span",
                { class: "nav-badge", title: t("board.lane.needs") },
                needs,
              )
            : null,
        ),
      ),
  );
}

/* Project switcher ----------------------------------------------------------- */

function renderProjects() {
  const select = byId("project-select");
  const options = projects().map((p) => h("option", { value: p.id }, p.name));
  if (looseRuns().length)
    options.push(h("option", { value: LOOSE }, t("project.loose")));
  if (!options.length)
    options.push(h("option", { value: "", disabled: true }, t("project.none")));
  replace(select, options);
  select.value = store.project ?? "";
  select.disabled = !hasScope();
  const project = currentProject();
  byId("project-path").textContent = project?.workspace || "";
  byId("project-path").title = project?.workspace || "";
  byId("project-edit").disabled = !project;
}

/* Queue and meters ----------------------------------------------------------- */

function renderQueue() {
  const state = store.state;
  const status = byId("queue-status");
  const toggle = byId("queue-toggle");
  if (!store.connected || !state) {
    status.className = "status-dot is-error";
    status.textContent = t("queue.offline");
    toggle.disabled = true;
    return;
  }
  const running = state.settings.running && !state.error;
  status.className =
    "status-dot " +
    (state.error ? "is-error" : running ? "is-running" : "is-paused");
  status.textContent = state.error
    ? t("queue.error")
    : running
      ? state.active_processes
        ? t("queue.working", { count: state.active_processes })
        : t("queue.running")
      : t("queue.paused");
  status.title = state.error || "";
  toggle.disabled = false;
  // Both labels share one cell, so switching them never moves the top bar.
  stableLabel(toggle, [t("queue.start"), t("queue.pause")], running ? 1 : 0);
  toggle.classList.toggle("primary-soft", !running);
  const banner = byId("banner");
  banner.hidden = !state.error;
  if (state.error)
    replace(
      banner,
      h("strong", {}, t("queue.errorTitle")),
      h("span", { class: "mono" }, state.error),
      h(
        "button",
        { type: "button", onclick: () => go("journal") },
        t("journal.open"),
      ),
    );
}

function meter(label, value, hint) {
  return h(
    "div",
    { title: hint || "" },
    h("dt", {}, label),
    h("dd", {}, value),
  );
}

function renderMeters() {
  const state = store.state;
  if (!state) return;
  const { totals, settings } = state;
  replace(
    byId("meters"),
    meter(
      t("meter.calls"),
      `${totals.calls} / ${settings.max_calls}`,
      t("meter.callsHint"),
    ),
    meter(
      t("meter.tokens"),
      formatNumber(totals.tokens) + (totals.usage_unknown ? "+" : ""),
      totals.usage_unknown ? t("meter.tokensUnknown") : "",
    ),
    meter(t("meter.cost"), money(state.usage?.total_usd), t("meter.costHint")),
  );
}

/* Wiring --------------------------------------------------------------------- */

function onStore(_, reason) {
  renderProjects();
  renderQueue();
  renderMeters();
  renderNav();
  byId("new-task").disabled = !store.connected || !currentProject();
  byId("new-task").title = currentProject() ? "" : t("task.needsProject");
  current?.update?.(store, reason);
}

export function startShell() {
  applyStatic();
  byId("language-select").value = window.ffaiPreferences.language;
  byId("theme-select").value = window.ffaiPreferences.theme;
  byId("language-select").onchange = (e) =>
    window.ffaiPreferences.set("language", e.target.value);
  byId("theme-select").onchange = (e) =>
    window.ffaiPreferences.set("theme", e.target.value);
  document.addEventListener("preferences-changed", () => {
    applyStatic();
    renderTitle();
    onStore(store, "language");
  });
  byId("project-select").onchange = (e) => selectProject(e.target.value);
  byId("nav-back").onclick = () =>
    depth > 0 ? history.back() : go("overview");
  byId("queue-toggle").onclick = toggleQueue;
  byId("close-app").onclick = closeApplication;
  window.addEventListener("popstate", (event) => {
    depth = event.state?.depth || 0;
    routeNow();
  });
  // A hand-edited address is a new place, not a history step.
  window.addEventListener("hashchange", routeNow);
  history.replaceState({ depth: 0 }, "", location.hash || "#overview");
  subscribe(onStore);
  routeNow();
  onStore(store, "mount");
}

async function closeApplication() {
  const ok = await confirmDialog({
    title: t("app.close"),
    message: t("app.closeConfirm"),
    confirm: t("app.close"),
    danger: true,
  });
  if (!ok) return;
  await attempt(async () => {
    await api.post("shutdown", {});
    stopPolling();
    document
      .querySelectorAll("button, select, input, textarea")
      .forEach((n) => (n.disabled = true));
    byId("queue-status").textContent = t("app.stopped");
    toast(t("app.stoppedHint"), { timeout: 0 });
  });
}
