"use strict";
const $ = (id) => document.getElementById(id);
let state = {},
  token = "",
  flow = null,
  selected = 0;
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
function notify(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
}
async function api(action, body) {
  const r = await fetch(
    "/api/" + action,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-FFAI-Token": token,
          },
          body: JSON.stringify(body),
        },
  );
  const v = await r.json();
  if (!r.ok) throw Error(v.error || "Запрос отклонён");
  return v;
}
async function act(fn) {
  try {
    notify("");
    await fn();
    await refresh();
  } catch (e) {
    notify(e.message, true);
  }
}
function showTab(name) {
  document
    .querySelectorAll(".tab")
    .forEach((n) => n.classList.toggle("hidden", n.id !== name));
  document
    .querySelectorAll("nav button")
    .forEach((n) => n.classList.toggle("active", n.dataset.tab === name));
}
document
  .querySelectorAll("[data-tab]")
  .forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
function option(value, label) {
  const n = el("option", label);
  n.value = value;
  return n;
}
function options(id, items) {
  const select = $(id),
    old = select.value;
  select.replaceChildren(...items);
  if ([...select.options].some((x) => x.value === old)) select.value = old;
}
let stateTag = null;
let stateBody = null;
// Polling revalidates with the last ETag; an unchanged board is not re-sent.
async function fetchState() {
  const r = await fetch("/api/state", {
    headers: stateTag ? { "If-None-Match": stateTag } : {},
  });
  if (r.status === 304 && stateBody) return stateBody;
  const v = await r.json();
  if (!r.ok) throw Error(v.error || "Запрос отклонён");
  stateTag = r.headers.get("ETag");
  stateBody = v;
  return v;
}
async function refresh() {
  try {
    state = await fetchState();
    token = state.token;
    if (!$("profiles-json").dataset.loaded) {
      $("profiles-json").value = JSON.stringify(state.profile_config, null, 2);
      $("profiles-json").dataset.loaded = "1";
    }
    $("connection").textContent = state.error
      ? "Ошибка исполнителя"
      : state.settings.running
        ? "Очередь включена"
        : "Очередь на паузе";
    $("connection").classList.toggle(
      "is-running",
      !state.error && state.settings.running,
    );
    $("connection").classList.toggle("is-error", !!state.error);
    $("queue").textContent = state.settings.running
      ? "Приостановить очередь"
      : "Запустить очередь";
    $("accepted").textContent = state.totals.accepted;
    $("calls").textContent =
      state.totals.calls + " / " + state.settings.max_calls;
    $("planning").textContent =
      state.totals.planning_calls + " / " + state.settings.max_planning_calls;
    $("tokens").textContent =
      state.totals.tokens.toLocaleString() +
      (state.totals.usage_unknown ? " + неизвестные" : "");
    $("cost").textContent = money(state.usage?.total_usd);
    $("last").textContent = state.last_transition
      ? new Date(state.last_transition * 1000).toLocaleString()
      : "—";
    if (state.error) notify(state.error, true);
    if (!boardInitialized) {
      boardInitialized = true;
    }
    renderBoard();
    renderOffice();
    renderInbox();
    renderWorkspace();
    if (detailId && !$("detail").classList.contains("hidden")) {
      const current = state.runs.find((r) => r.id === detailId);
      if (current && current.version !== detailVersion)
        await openDetail(detailId, false);
    }
    options(
      "definitions",
      state.definitions.map((x) =>
        option(x.digest, x.workflow.id + " · " + x.digest.slice(0, 8)),
      ),
    );
    options("saved-flows", [
      option("", "Выберите…"),
      ...state.definitions.map((x) =>
        option(x.digest, x.workflow.id + " · " + x.digest.slice(0, 8)),
      ),
    ]);
    $("handlers").replaceChildren(
      ...state.profiles.map((x) => option(x.id, x.id)),
    );
    renderProfiles();
    $("profile-names").replaceChildren(
      ...Object.keys(state.profile_config.profiles || {}).map((name) =>
        option(name, name),
      ),
    );
    document.dispatchEvent(new Event("ffai-refreshed"));
    if (!document.activeElement.closest("#budget-form"))
      for (const name of ["max_calls", "max_planning_calls"])
        $("budget-form").elements[name].value = state.settings[name];
  } catch (e) {
    $("connection").textContent = "Нет связи";
    notify(e.message, true);
  }
}
const labels = {
  spec: "Спецификация",
  tickets: "Декомпозиция",
  implement: "Разработка",
  checks: "Проверки",
  review: "Ревью",
  diagnose: "Диагностика",
  repair: "Исправление",
  reconcile: "Сверка состояния",
  interview: "Ваш ответ",
  approve: "Согласование",
  accepted: "Приёмка",
  finish: "Готово",
  ask: "Вопросы",
  answer: "Ваш ответ",
  integrate: "Слияние",
  rebase: "Перебазирование",
  resolve: "Разрешение конфликта",
  merge_conflict: "Конфликт слияния",
};
const roles = {
  spec: "Аналитик",
  tickets: "Планировщик",
  implement: "Разработчик",
  checks: "Тестировщик",
  review: "Ревьюер",
  diagnose: "Диагност",
  repair: "Исправление",
  reconcile: "Сверка",
  interview: "Вы",
  approve: "Вы",
};
let boardInitialized = false;
let boardMode = recall("board-mode", "live"),
  boardSignature = "",
  officeSignature = "",
  inboxSignature = "";
let detailId = null,
  detailVersion = null,
  detailRequest = 0;
const answerDrafts = persistentMap("answer-drafts");
function stepFor(run) {
  return state.definitions
    .find((d) => d.digest === run.workflow_digest)
    ?.workflow.steps.find((s) => s.id === run.step);
}
function laneFor(r) {
  if (r.status === "accepted") return "accepted";
  if (
    r.status === "blocked" ||
    r.status === "waiting" ||
    (r.active && stepFor(r)?.kind === "human")
  )
    return "blocked";
  return r.active || r.status === "running" || r.paused === false
    ? "running"
    : "ready";
}
const KINDS = {
  requirement: { label: "Требование", glyph: "◇" },
  ticket: { label: "Тикет", glyph: "▣" },
  task: { label: "Задание", glyph: "○" },
};
let kindFilter = recall("kind-filter", "");
const openPlans = new Set(recall("open-plans", []));
function money(value) {
  if (value === undefined || value === null) return "—";
  const digits = value >= 100 ? 0 : value >= 1 ? 2 : 3;
  return "$" + value.toFixed(digits);
}
function kindOf(run) {
  const kind = state.task_metadata?.[run.id]?.kind;
  return KINDS[kind] ? kind : "task";
}
function workflowOf(run) {
  return state.definitions.find((d) => d.digest === run.workflow_digest)
    ?.workflow;
}
/* Who executes a step, e.g. "claude · opus"; checks run commands, humans are you. */
function runnerLabel(step) {
  if (!step) return "";
  if (step.kind === "human") return tr("вы");
  if (step.kind === "check" || step.kind === "operation")
    return step.handler || "command";
  if (step.kind !== "agent") return "";
  const name = step.profile !== "default" ? step.profile : step.handler;
  const profile = state.profile_config?.profiles?.[name];
  if (!profile) return name;
  return name + " · " + profile.model;
}
function planTitle(project, planId) {
  const plans =
    state.plans?.[project] || Object.values(state.plans || {}).flat();
  return plans.find((p) => p.id === planId)?.title || "План " + planId;
}
function statusPill(r) {
  const key = laneFor(r);
  const text =
    {
      ready: r.paused ? "пауза" : "в очереди",
      running: "в работе",
      blocked:
        r.active && stepFor(r)?.kind === "human" ? "нужен ответ" : "нужны вы",
      accepted: "готово",
    }[key] || r.status;
  return el("span", text, "status-pill status-" + key);
}
function taskCard(r) {
  const kind = kindOf(r);
  const meta = state.task_metadata?.[r.id] || {};
  const c = el("button", undefined, "card kind-" + kind);
  configureDraggable(c, r);
  const head = el("span", undefined, "card-head");
  head.append(
    el("span", KINDS[kind].glyph + " " + KINDS[kind].label, "kind-tag"),
  );
  if (meta.plan) head.append(raw("span", meta.plan, "plan-chip"));
  head.append(statusPill(r));
  c.append(head, raw("strong", r.title || r.id));
  const step = stepFor(r);
  const line = el("small", undefined, "card-step");
  line.append(
    el("span", tr(labels[r.step] || r.step)),
    raw("span", runnerLabel(step), "runner"),
  );
  c.append(line);
  const facts = [];
  if (kind === "ticket") {
    const checks = workflowOf(r)?.steps.filter(
      (s) => s.kind === "check",
    ).length;
    if (checks) facts.push(checks + " " + tr("пров."));
    if (meta.parent) facts.push(tr("из") + " " + meta.parent);
  }
  if (kind === "requirement") {
    const children = state.runs.filter(
      (x) => state.task_metadata?.[x.id]?.parent === r.id,
    ).length;
    if (children) facts.push(children + " " + tr("тикетов"));
  }
  if (r.calls) facts.push(r.calls + " " + tr("вызовов"));
  const spent = state.usage?.per_run_usd?.[r.id];
  if (spent) facts.push("≈ " + money(spent));
  if (facts.length) c.append(raw("small", facts.join(" · "), "card-facts"));

  if (r.reason && r.status !== "accepted")
    c.append(raw("small", r.reason, "card-reason"));
  c.onclick = () => act(() => openDetail(r.id));
  return c;
}
function renderPlans(items) {
  const root = el("div", undefined, "plans");
  const byPlan = new Map();
  for (const r of items) {
    const plan = state.task_metadata?.[r.id]?.plan || "";
    if (!byPlan.has(plan)) byPlan.set(plan, []);
    byPlan.get(plan).push(r);
  }
  const known = (state.plans?.[currentProject] || []).filter((p) =>
    byPlan.has(p.id),
  );
  const ids = [
    ...known.map((p) => p.id),
    ...[...byPlan.keys()].filter((id) => !known.some((p) => p.id === id)),
  ];
  if (!ids.length)
    root.append(
      el("p", "Планов нет. Задания без плана видны на доске.", "empty"),
    );
  for (const id of ids) {
    const runs = byPlan.get(id) || [];
    const info = known.find((p) => p.id === id);
    const accepted = runs.filter((r) => r.status === "accepted").length;
    const total = info ? info.requirements + info.tickets : runs.length;
    const done = (info?.accepted || 0) + accepted;
    const section = el("details", undefined, "plan");
    section.open = openPlans.has(id);
    section.ontoggle = () => {
      if (section.open) {
        openPlans.add(id);
        remember("open-plans", [...openPlans]);
        if (!section.querySelector(".plan-tree"))
          section.append(planTree(runs));
      } else {
        openPlans.delete(id);
        remember("open-plans", [...openPlans]);
      }
    };
    const summary = el("summary");
    const bar = el("span", undefined, "plan-bar");
    const fill = el("span");
    fill.style.width = Math.round((100 * done) / Math.max(1, total)) + "%";
    bar.append(fill);
    const counts = el("span", undefined, "plan-counts");
    const req = runs.filter((r) => kindOf(r) === "requirement").length;
    const tix = runs.filter((r) => kindOf(r) === "ticket").length;
    const waiting = runs.filter((r) => laneFor(r) === "blocked").length;
    counts.append(
      el("span", req + " " + tr("треб.")),
      el("span", tix + " " + tr("тик.")),
    );
    if (waiting)
      counts.append(el("span", waiting + " " + tr("ждут вас"), "attention"));
    summary.append(
      raw(
        "span",
        id ? planTitle(currentProject, id) : tr("Без плана"),
        "plan-name",
      ),
      bar,
      raw("span", done + " / " + total, "plan-progress"),
      counts,
    );
    section.append(summary);
    if (section.open) section.append(planTree(runs));
    root.append(section);
  }
  return root;
}
function planTree(runs) {
  const tree = el("div", undefined, "plan-tree");
  const children = new Map();
  for (const r of runs) {
    const parent = state.task_metadata?.[r.id]?.parent;
    if (kindOf(r) === "ticket" && parent) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(r);
    }
  }
  const requirements = runs.filter((r) => kindOf(r) === "requirement");
  const shown = new Set();
  const row = (r, depth) => {
    shown.add(r.id);
    const item = el("button", undefined, "tree-row kind-" + kindOf(r));
    item.style.setProperty("--depth", depth);
    item.append(
      el("span", KINDS[kindOf(r)].glyph, "tree-glyph"),
      raw(
        "span",
        r.title || state.task_metadata?.[r.id]?.title || r.id,
        "tree-title",
      ),
      raw("span", runnerLabel(stepFor(r)), "runner"),
      statusPill(r),
    );
    item.onclick = () => act(() => openDetail(r.id));
    return item;
  };
  for (const r of requirements) {
    tree.append(row({ ...r, title: state.task_metadata?.[r.id]?.title }, 0));
    for (const t of children.get(r.id) || [])
      tree.append(row({ ...t, title: state.task_metadata?.[t.id]?.title }, 1));
  }
  // Tickets whose requirement was decomposed before the move group under its id.
  const orphans = new Map();
  for (const r of runs)
    if (!shown.has(r.id)) {
      const parent = state.task_metadata?.[r.id]?.parent || "";
      if (!orphans.has(parent)) orphans.set(parent, []);
      orphans.get(parent).push(r);
    }
  for (const [parent, list] of orphans) {
    if (parent) {
      const head = el("div", undefined, "tree-group");
      head.append(
        el("span", "◇", "tree-glyph"),
        raw("span", parent),
        el("span", tr("разбито на тикеты"), "hint"),
      );
      tree.append(head);
    }
    for (const t of list)
      tree.append(
        row(
          { ...t, title: state.task_metadata?.[t.id]?.title },
          parent ? 1 : 0,
        ),
      );
  }
  return tree;
}
function renderBoard() {
  const live = state.runs.filter(matchesProject).map((r) => ({
    ...r,
    title: state.task_metadata?.[r.id]?.title || r.id,
  }));
  const query = $("task-search").value.toLowerCase();
  const plan = $("plan-filter").value || recall("plan-filter", "");
  const items = live.filter(
    (r) =>
      (!plan || state.task_metadata?.[r.id]?.plan === plan) &&
      (!kindFilter || kindOf(r) === kindFilter) &&
      [r.id, r.title, r.context, r.reason]
        .join(" ")
        .toLowerCase()
        .includes(query),
  );
  const signature = JSON.stringify([
    boardMode,
    items,
    query,
    plan,
    kindFilter,
    currentProject,
    state.definitions.length,
    state.profile_config,
  ]);
  if (signature === boardSignature) return;
  boardSignature = signature;
  const planIds = [
    ...new Set(
      live.map((r) => state.task_metadata?.[r.id]?.plan).filter(Boolean),
    ),
  ];
  options("plan-filter", [
    option("", "Все планы"),
    ...planIds.sort().map((p) => option(p, planTitle(currentProject, p))),
  ]);
  const wantedPlan = recall("plan-filter", "");
  if (wantedPlan && !$("plan-filter").value && planIds.includes(wantedPlan))
    $("plan-filter").value = wantedPlan;
  $("plan-filter").disabled = !planIds.length;
  const counts = { requirement: 0, ticket: 0, task: 0 };
  for (const r of live) counts[kindOf(r)]++;
  const chips = $("kind-filter");
  chips.replaceChildren();
  chips.hidden = counts.requirement + counts.ticket === 0;
  for (const [key, label] of [
    ["", "Все"],
    ["requirement", "Требования"],
    ["ticket", "Тикеты"],
    ["task", "Задания"],
  ]) {
    if (key && !counts[key]) continue;
    const b = el("button", undefined, "chip" + (key ? " kind-" + key : ""));
    b.type = "button";
    b.setAttribute("aria-pressed", String(kindFilter === key));
    b.append(
      el("span", label),
      el("span", String(key ? counts[key] : live.length), "chip-count"),
    );
    b.onclick = () => {
      kindFilter = key;
      remember("kind-filter", key);
      renderBoard();
    };
    chips.append(b);
  }
  $("board").replaceChildren();
  $("board").classList.toggle("as-plans", boardMode === "plans");
  if (boardMode === "plans") {
    $("board").append(renderPlans(items));
    return;
  }
  for (const [key, title] of [
    ["ready", "К выполнению"],
    ["running", "В работе"],
    ["blocked", "Нужны вы"],
    ["accepted", "Готово"],
  ]) {
    const runs = items.filter((r) => laneFor(r) === key);
    const lane = el("section", undefined, "lane lane-" + key);
    configureDropTarget(lane, key);
    const heading = el("h2", title);
    heading.append(el("span", runs.length, "lane-count"));
    lane.append(heading);
    for (const r of runs.slice(0, 40)) lane.append(taskCard(r));
    if (runs.length > 40)
      lane.append(
        el(
          "p",
          `Ещё ${runs.length - 40}. Выберите план, тип или уточните поиск.`,
          "hint",
        ),
      );
    if (!runs.length)
      lane.append(
        el(
          "div",
          query || plan || kindFilter
            ? "Нет совпадений"
            : {
                ready: "Здесь появятся ваши задачи",
                running: "Команда ждёт задания",
                blocked: "Ответы пока не нужны",
                accepted: "Проверенные результаты будут здесь",
              }[key],
          "empty",
        ),
      );
    $("board").append(lane);
  }
}
function appendClose(root) {
  const close = el("button", "Закрыть карточку", "detail-close");
  close.onclick = () => {
    root.classList.add("hidden");
    detailId = null;
    $("task-dialog").close();
  };
  root.prepend(close);
}
async function openMainFlow(stepId) {
  if (!flow) {
    flow = await api("template", {
      name: "main-flow",
      project: currentProject,
      language: window.ffaiPreferences.language,
    });
    selected = 0;
  }
  if (stepId)
    selected = Math.max(
      0,
      flow.steps.findIndex((s) => s.id === stepId),
    );
  showTab("flows");
  renderFlow();
  $("graph-fit").click();
}
let officeMounted = false,
  officeWorkflow = null;
function officeFlow() {
  // Every workflow the project uses, most frequent first; the editor draft otherwise.
  const runs = state.runs.filter(matchesProject);
  const counts = new Map();
  for (const r of runs)
    counts.set(r.workflow_digest, (counts.get(r.workflow_digest) || 0) + 1);
  const seen = new Set();
  const flows = [];
  for (const [digest] of [...counts.entries()].sort((a, b) => b[1] - a[1])) {
    const workflow = state.definitions.find(
      (d) => d.digest === digest,
    )?.workflow;
    const shape = workflow && workflow.steps.map((s) => s.id + s.kind).join();
    if (workflow && !seen.has(shape)) {
      seen.add(shape);
      flows.push(workflow);
    }
  }
  return flows.length ? flows : flow ? [flow] : [];
}
function stepLabel(step) {
  let title = "";
  try {
    title = JSON.parse(step.config || "{}").title || "";
  } catch {}
  return tr(labels[step.id] || title || step.id);
}
function renderOffice() {
  if (!officeMounted) {
    officeMounted = true;
    window.ffaiOffice.mount($("office"), {
      flow: () => officeWorkflow,

      // Desk caption: the role for known steps, otherwise the step's own name.
      label: (step) =>
        step.kind === "human"
          ? tr("Вы")
          : step.kind === "check"
            ? tr("Тестировщик")
            : step.handler?.startsWith("lane-")
              ? tr("Интегратор")
              : tr(roles[step.id] || "") || stepLabel(step),
      runner: (step) =>
        step.kind === "check"
          ? tr("проверки проекта")
          : step.handler?.startsWith("lane-")
            ? "git · fast-forward"
            : runnerLabel(step),
      summary: (tasks, people) =>
        tr("Сотрудников") +
        ": " +
        people +
        ", " +
        tr("документов") +
        ": " +
        tasks,
      inboxLabel: () => tr("Входящие"),
      shelfLabel: () => tr("Готово"),
      docsLabel: (runs) =>
        runs
          .slice(0, 3)
          .map((r) => state.task_metadata?.[r.id]?.title || r.id)
          .join(" · ") + (runs.length > 3 ? " · +" + (runs.length - 3) : ""),
      emptyLabel: () => tr("Откройте сценарий, чтобы расставить столы"),
      deskHint: (n) =>
        n ? n + " " + tr("в работе") : tr("свободен · открыть шаг в редакторе"),
      openRun: (run) => act(() => openDetail(run.id)),
      openStep: (step) =>
        act(async () => {
          flow = structuredClone(
            officeWorkflow.find((f) => f.steps.some((s) => s.id === step.id)) ||
              officeWorkflow[0],
          );
          selected = Math.max(
            0,
            flow.steps.findIndex((s) => s.id === step.id),
          );
          showTab("flows");
          renderFlow();
        }),
    });
  }
  officeWorkflow = officeFlow();
  const runs = state.runs.filter(matchesProject);
  const signature = JSON.stringify([
    runs,
    officeWorkflow.map((f) => f.id + f.steps.length),
  ]);
  if (signature === officeSignature) return;
  officeSignature = signature;
  window.ffaiOffice.update(runs, stepFor, officeWorkflow);
}
function renderInbox() {
  const runs = state.runs
    .filter(matchesProject)
    .filter((r) => r.active && stepFor(r)?.kind === "human");
  const signature = JSON.stringify(runs);
  if (signature === inboxSignature) return;
  inboxSignature = signature;
  $("inbox").classList.toggle("hidden", !runs.length);
  $("inbox").replaceChildren(
    el("strong", `Команда ждёт вашего ответа · ${runs.length}`),
  );
  for (const r of runs) {
    const b = el("button", r.id + " → Ответить");
    b.onclick = () => act(() => openDetail(r.id));
    $("inbox").append(b);
  }
}
for (const [id, mode] of [
  ["live-board", "live"],
  ["plans-board", "plans"],
])
  $(id).onclick = () => {
    boardMode = mode;
    remember("board-mode", mode);
    document
      .querySelectorAll(".segmented button")
      .forEach((b) => b.classList.toggle("active", b.id === id));
    renderBoard();
  };
$("task-search").value = recall("task-search", "");
$("task-search").oninput = () => {
  remember("task-search", $("task-search").value);
  renderBoard();
};
$("plan-filter").onchange = () => {
  remember("plan-filter", $("plan-filter").value);
  renderBoard();
};
for (const b of document.querySelectorAll(".segmented button"))
  b.classList.toggle(
    "active",
    b.id === (boardMode === "plans" ? "plans-board" : "live-board"),
  );
$("template").value = recall("template", $("template").value);
$("template").addEventListener("change", () =>
  remember("template", $("template").value),
);
$("edit-main-flow").onclick = () => act(() => openMainFlow());
async function openDetail(id) {
  const request = ++detailRequest;
  const d = await api("run?id=" + encodeURIComponent(id));
  if (request !== detailRequest) return;
  const r = d.run,
    root = $("detail"),
    step = d.workflow.steps.find((s) => s.id === r.step);
  detailId = id;
  detailVersion = r.version;
  showTaskDialog();
  root.classList.remove("hidden");
  root.replaceChildren(
    raw("h2", d.metadata?.title || r.id),
    raw("small", r.id, "eyebrow"),
    el(
      "p",
      (labels[r.step] || r.step) +
        " · " +
        ({
          ready: "К выполнению",
          running: step.kind === "human" ? "Нужен ваш ответ" : "В работе",
          accepted: "Готово",
          blocked: "Заблокировано",
          waiting: "Ожидание",
        }[r.status] || r.status),
    ),
  );
  appendClose(root);
  const actions = el("div", undefined, "detail-actions");
  for (const [cmd, label] of [
    ["resume", "Продолжить"],
    ["pause", "Пауза"],
    ["stop", "Остановить"],
    ["retry", "Повторить"],
    ["recover", "Сверить и восстановить"],
  ]) {
    if (
      r.status === "accepted" ||
      (cmd === "resume" && (!r.paused || r.status === "blocked")) ||
      (cmd === "pause" && r.paused) ||
      (cmd === "stop" && !r.active) ||
      (cmd === "retry" && (r.status !== "blocked" || r.active)) ||
      (cmd === "recover" &&
        (r.active ||
          !["blocked", "waiting"].includes(r.status) ||
          !JSON.parse(step.config || "{}").recovery_step))
    )
      continue;
    const b = el("button", label);
    b.onclick = () =>
      act(async () => {
        await api(cmd, {
          id,
          version: r.version,
          request_id: crypto.randomUUID(),
        });
        await openDetail(id);
      });
    actions.append(b);
  }
  actions.append(autoAnswerSwitch(r, id));
  const reload = el("button", "Обновить");
  reload.onclick = () => act(() => openDetail(id));
  actions.append(reload);
  root.append(actions);
  const track = el("div", undefined, "run-pipeline");
  track.setAttribute("aria-label", tr("Путь задания по сценарию"));
  window.ffaiPipeline.render(track, d.workflow, {
    labels,
    runner: runnerLabel,
    zoom: 0.62,
    run: r,
    recoveryLabel: tr("Восстановление и вопросы"),
    ariaLabel: tr("Путь задания по сценарию"),
  });
  root.append(track);
  if (d.lane?.root) root.append(laneInfo(d.lane));
  const tabs = el("div", undefined, "detail-tabs"),
    discussion = el("section"),
    details = el("section"),
    events = el("section");
  const panes = { discussion, details, events };
  const choose = (key) => {
    detailTab = key;
    remember("detail-tab", key);
    Object.entries(panes).forEach(([name, node]) =>
      node.classList.toggle("hidden", name !== key),
    );
    [...tabs.children].forEach((b) =>
      b.classList.toggle("active", b.dataset.pane === key),
    );
  };
  for (const [key, label] of [
    ["discussion", "Обсуждение"],
    ["details", "Детали"],
    ["events", "События"],
  ]) {
    const b = el("button", label);
    b.dataset.pane = key;
    b.onclick = () => choose(key);
    tabs.append(b);
  }
  root.append(tabs, discussion, details, events);
  if (r.reason) discussion.append(raw("p", r.reason, "question-thread"));
  for (const result of [...d.results].reverse()) {
    const item = el("article", undefined, "message-bubble");
    item.append(raw("small", result.outcome), raw("p", result.reason));
    discussion.append(item);
  }
  for (const event of d.events.filter((e) => e.kind === "operator_message")) {
    const message = JSON.parse(event.detail),
      item = el("article", undefined, "message-bubble operator-message");
    item.append(
      el("small", "Вы"),
      raw("p", message.text),
      el(
        "small",
        r.generation > message.after_generation
          ? "Следующий запуск создан"
          : "Сохранено для следующего шага",
      ),
    );
    discussion.append(item);
  }
  if (r.active && step.kind === "human") {
    discussion.append(questionPanel(d, r, step, id));
  } else if (r.status !== "accepted") {
    const form = el("form", undefined, "message-form"),
      input = el("textarea");
    input.setAttribute("aria-label", "Сообщение агенту");
    input.placeholder = "Сообщение агенту";
    input.required = true;
    input.value = messageDrafts.get(id) || "";
    input.oninput = () => messageDrafts.set(id, input.value);
    const send = el("button", "Сохранить для следующего шага", "primary");
    form.append(input, send);
    form.onsubmit = (e) => {
      e.preventDefault();
      act(async () => {
        send.disabled = true;
        try {
          await api("message", {
            id,
            version: r.version,
            message: input.value,
            request_id: crypto.randomUUID(),
          });
          messageDrafts.delete(id);
          await openDetail(id);
          notify("Сообщение сохранено. Агент получит его в следующем запуске.");
        } finally {
          send.disabled = false;
        }
      });
    };
    discussion.append(form);
  }
  details.append(
    raw("p", d.context),
    el("h3", "Инструкция"),
    raw("pre", step.prompt),
    el("h3", "Последние результаты"),
    raw("pre", JSON.stringify(d.results, null, 2)),
  );
  events.append(
    el("h3", "История"),
    raw(
      "pre",
      d.events
        .map(
          (e) =>
            new Date(e.at * 1000).toLocaleTimeString() +
            " · " +
            e.kind +
            " · " +
            e.detail,
        )
        .join("\n"),
    ),
  );
  choose(detailTab);
}
$("queue").onclick = () =>
  act(() => api("queue", { running: !state.settings.running }));
$("new-task").onclick = () => $("task-form").classList.toggle("hidden");
$("task-form").onsubmit = (e) => {
  e.preventDefault();
  act(async () => {
    const doc = Object.fromEntries(new FormData(e.target));
    doc.project = currentProject;
    doc.dependencies = doc.dependencies
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const r = await api("create", doc);
    $("live-board").click();
    notify("Создано на паузе: " + r.id);
    $("task-form").classList.add("hidden");
    await openDetail(r.id);
  });
};
function captureFlow() {
  if (!flow) throw Error("Откройте шаблон");
  flow.id = $("flow-id").value;
  flow.entry = $("flow-entry").value;
  flow.max_calls = Number($("flow-calls").value);
  flow.max_tokens = $("flow-tokens").value
    ? Number($("flow-tokens").value)
    : null;
  flow.max_planning_calls =
    $("flow-planning").value === "" ? null : Number($("flow-planning").value);
  return flow;
}
function renderFlow() {
  if (!flow) return;
  // The editor draft is kept automatically; publishing stays an explicit step.
  try {
    localStorage.setItem("ffai-flow-draft", JSON.stringify({ flow }));
  } catch {}
  $("flow-id").value = flow.id;
  $("flow-entry").value = flow.entry;
  $("flow-calls").value = flow.max_calls;
  $("flow-tokens").value = flow.max_tokens ?? "";
  $("flow-planning").value = flow.max_planning_calls ?? "";
  renderGraph();
  $("inspector-title").textContent =
    labels[flow.steps[selected]?.id] ||
    flow.steps[selected]?.id ||
    "Выберите шаг";
  for (const id of ["edge-source", "edge-target"])
    options(
      id,
      flow.steps.map((s) => option(s.id, labels[s.id] || s.id)),
    );
  $("flow-json").value = JSON.stringify(flow, null, 2);
  const step = flow.steps[selected];
  if (step)
    for (const n of $("step-form").elements) {
      if (!n.name) continue;
      if (n.type === "checkbox") n.checked = !!step[n.name];
      else
        n.value =
          n.name === "transitions"
            ? step.transitions.map((p) => p.join("=")).join(", ")
            : (step[n.name] ?? (n.name === "profile" ? "default" : ""));
    }
}
$("load-template").onclick = () =>
  act(async () => {
    flow = await api("template", {
      name: $("template").value,
      project: currentProject,
      language: window.ffaiPreferences.language,
    });
    selected = 0;
    renderFlow();
    notify("Черновик открыт. Проверьте профили и команду тестов.");
    $("graph-fit").click();
  });
$("saved-flows").onchange = (e) => {
  const item = state.definitions.find((d) => d.digest === e.target.value);
  if (item) {
    flow = structuredClone(item.workflow);
    selected = 0;
    renderFlow();
  }
};
$("step-form").onsubmit = (e) => {
  e.preventDefault();
  act(async () => {
    captureFlow();
    const doc = Object.fromEntries(new FormData(e.target));
    JSON.parse(doc.config || "{}");
    const old = flow.steps[selected];
    if (!old) throw Error("Выберите шаг");
    if (flow.steps.some((s, i) => i !== selected && s.id === doc.id))
      throw Error("Такой ID шага уже существует");
    const edges = doc.transitions.trim()
      ? doc.transitions.split(",").map((part) => {
          const pair = part.split("=").map((s) => s.trim());
          if (pair.length !== 2 || !pair[0] || !pair[1])
            throw Error("Переход: outcome=step");
          return pair;
        })
      : [];
    flow.steps[selected] = {
      ...old,
      ...doc,
      timeout: Number(doc.timeout),
      max_visits: Number(doc.max_visits),
      profile: doc.profile || "default",
      config: doc.config || "{}",
      transitions: edges,
      required: e.target.required.checked,
      gate: e.target.gate.checked,
      mutates: e.target.mutates.checked,
    };
    if (old.id !== doc.id) {
      flow.steps.forEach(
        (s) =>
          (s.transitions = s.transitions.map(([o, t]) => [
            o,
            t === old.id ? doc.id : t,
          ])),
      );
      if (flow.entry === old.id) flow.entry = doc.id;
      for (const step of flow.steps) {
        const config = JSON.parse(step.config || "{}");
        if (config.recovery_step === old.id) {
          config.recovery_step = doc.id;
          step.config = JSON.stringify(config);
        }
      }
    }
    renderFlow();
    notify("Шаг обновлён в черновике");
  });
};
$("add-step").onclick = () =>
  act(async () => {
    captureFlow();
    let i = flow.steps.length;
    while (flow.steps.some((s) => s.id === "step" + i)) i++;
    flow.steps.push({
      id: "step" + i,
      kind: "agent",
      handler: "",
      profile: "default",
      prompt: "",
      transitions: [],
      config: "{}",
      timeout: 900,
      max_visits: 1,
      required: false,
      gate: false,
      mutates: false,
    });
    selected = flow.steps.length - 1;
    renderFlow();
  });
$("delete-step").onclick = () =>
  act(async () => {
    captureFlow();
    const removed = flow.steps[selected]?.id;
    flow.steps.splice(selected, 1);
    flow.steps.forEach(
      (s) =>
        (s.transitions = s.transitions.filter(
          ([, target]) => target !== removed,
        )),
    );
    if (flow.entry === removed) flow.entry = flow.steps[0]?.id || "";
    selected = 0;
    renderFlow();
    notify("Шаг удалён. Исправьте оставшиеся переходы перед публикацией.");
  });
$("import-json").onclick = () =>
  act(async () => {
    flow = JSON.parse($("flow-json").value);
    selected = 0;
    renderFlow();
  });
for (const action of ["validate", "publish"])
  $(action).onclick = () =>
    act(async () => {
      const result = await api(action, { workflow: captureFlow() });
      notify(
        action === "publish"
          ? "Версия опубликована: " + result.digest.slice(0, 12)
          : "Граф и профили проверены. Вызовов моделей: 0.",
      );
    });
$("budget-form").onsubmit = (e) => {
  e.preventDefault();
  act(() =>
    api("budget", {
      max_calls: Number(e.target.max_calls.value),
      max_planning_calls: Number(e.target.max_planning_calls.value),
    }),
  );
};
$("save-profiles").onclick = () =>
  act(async () => {
    await api("profiles", { config: JSON.parse($("profiles-json").value) });
    notify(
      "Профили сохранены. Настройки уже созданных заданий остаются фиксированными.",
    );
  });
document.addEventListener("DOMContentLoaded", () => {
  refresh().then(async () => {
    document.dispatchEvent(new Event("ffai-ready"));
    try {
      const saved = localStorage.getItem("ffai-flow-draft");
      if (saved) {
        const draft = JSON.parse(saved);
        flow = draft.flow;
        selected = 0;
        renderFlow();
      } else {
        flow = await api("template", {
          name: "main-flow",
          project: currentProject,
          language: window.ffaiPreferences.language,
        });
        renderFlow();
      }
    } catch (error) {
      notify("Не удалось открыть черновик: " + error.message, true);
    }
  });
});
const pollTimer = setInterval(refresh, 3000);
$("close-app").onclick = async () => {
  try {
    await api("shutdown", {});
    clearInterval(pollTimer);
    document
      .querySelectorAll("button")
      .forEach((button) => (button.disabled = true));
    $("connection").textContent = "Приложение остановлено";
    notify("Можно закрыть вкладку. Ярлык снова запустит приложение.");
  } catch (error) {
    notify(error.message, true);
  }
};

$("profile-form").onsubmit = (e) => {
  e.preventDefault();
  act(async () => {
    const p = Object.fromEntries(new FormData(e.target)),
      config = JSON.parse($("profiles-json").value);
    config.schema = 1;
    config.runners ??= {};
    config.profiles ??= {};
    const runner = p.name + "-runner";
    config.runners[runner] = {
      adapter: p.adapter,
      executable: p.executable,
      arguments: JSON.parse(p.arguments),
    };
    config.profiles[p.name] = {
      runner,
      model: p.model,
      permissions: p.permissions,
      timeout_seconds: Number(p.timeout),
    };
    $("profiles-json").value = JSON.stringify(config, null, 2);
    notify("Профиль добавлен в черновик. Нажмите «Проверить и сохранить».");
  });
};
let graphZoom = 1,
  connecting = null,
  selectedEdge = null,
  graphSize = { width: 1, height: 1 };
const ns = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs = {}, text) {
  const n = document.createElementNS(ns, tag);
  Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v));
  if (text !== undefined) n.textContent = text;
  return n;
}
function defaultOutcome(step) {
  // Never propose an outcome the step already routes: connecting must not rewire it.
  const used = new Set(step.transitions.map(([o]) => o));
  const first =
    step.kind === "condition"
      ? "true"
      : step.gate
        ? "passed"
        : step.kind === "human"
          ? "answered"
          : "done";
  const names = [
    first,
    step.kind === "condition" ? "false" : "failed",
    "questions",
    "retry",
  ];
  for (let n = 2; names.length < 50; n++) names.push("outcome" + n);
  return names.find((name) => !used.has(name));
}
function renderGraph() {
  // Keep keyboard focus on the same stage or wire across re-renders.
  const focused = $("graph").contains(document.activeElement)
    ? document.activeElement.getAttribute("aria-label")
    : null;
  renderPipeline();
  if (focused)
    (
      $("graph").querySelector(`[aria-label="${CSS.escape(focused)}"]`) ||
      $("graph")
    ).focus();
}
function renderPipeline() {
  graphSize = window.ffaiPipeline.render($("graph"), flow, {
    labels,
    runner: runnerLabel,
    selected,
    zoom: graphZoom,
    connecting,
    selectedEdge,
    recoveryLabel: tr("Восстановление и вопросы"),
    ariaLabel: tr("Пайплайн сценария"),
    onSelect: (index) => {
      if (connecting) {
        $("edge-source").value = connecting;
        $("edge-target").value = flow.steps[index].id;
        connecting = null;
        $("edge-form").requestSubmit();
        return;
      }
      captureFlow();
      selected = index;
      selectedEdge = null;
      renderFlow();
    },
    onEdge: (source, outcome, target) => {
      selectedEdge = [source, outcome];
      $("edge-source").value = source;
      $("edge-target").value = target;
      $("edge-outcome").value = outcome;
      renderGraph();
      notify(tr("Связь выбрана: Delete удаляет её, форма ниже меняет цель."));
    },
    onInsert: (source, outcome) =>
      act(async () => {
        captureFlow();
        const from = flow.steps.find((s) => s.id === source);
        const edge = from.transitions.find(([o]) => o === outcome);
        let n = flow.steps.length;
        while (flow.steps.some((s) => s.id === "step" + n)) n++;
        const id = "step" + n;
        flow.steps.push({
          id,
          kind: "agent",
          handler: "",
          profile: "default",
          prompt: "",
          transitions: [["done", edge[1]]],
          config: "{}",
          timeout: 900,
          max_visits: 3,
          required: false,
          gate: false,
          mutates: false,
          condition_key: "",
          condition_value: "",
        });
        edge[1] = id;
        selected = flow.steps.length - 1;
        selectedEdge = null;
        renderFlow();
        notify(
          tr("Шаг вставлен. Выберите исполнителя и инструкцию в инспекторе."),
        );
      }),
    onPort: (source) => {
      connecting = connecting === source ? null : source;
      selectedEdge = null;
      if (connecting) {
        const step = flow.steps.find((s) => s.id === source);
        $("edge-source").value = source;
        $("edge-outcome").value = defaultOutcome(step);
      }
      renderGraph();
      if (connecting)
        notify(
          tr("Выберите шаг, куда ведёт результат") +
            " «" +
            $("edge-outcome").value +
            "». Esc — " +
            tr("отмена"),
        );
    },
    onBackground: () => {
      if (!connecting && !selectedEdge) return;
      connecting = null;
      selectedEdge = null;
      renderGraph();
    },
  });
}
$("graph").addEventListener("keydown", (e) => {
  if (e.key === "Escape" && (connecting || selectedEdge)) {
    connecting = null;
    selectedEdge = null;
    renderGraph();
  } else if ((e.key === "Delete" || e.key === "Backspace") && selectedEdge)
    $("edge-delete").click();
});
$("zoom-in").onclick = () => {
  graphZoom = Math.min(1.6, graphZoom + 0.1);
  renderGraph();
};
$("zoom-out").onclick = () => {
  graphZoom = Math.max(0.4, graphZoom - 0.1);
  renderGraph();
};
$("graph-fit").onclick = () => {
  graphZoom = Math.max(
    0.6,
    Math.min(1, ($("graph").clientWidth - 16) / graphSize.width),
  );
  renderGraph();
};
$("draft-save").onclick = () =>
  act(async () => {
    localStorage.setItem(
      "ffai-flow-draft",
      JSON.stringify({ flow: captureFlow() }),
    );
    notify(
      "Черновик сохранён в этом браузере. Для запуска опубликуйте проверенную версию.",
    );
  });
$("edge-form").onsubmit = (e) => {
  e.preventDefault();
  act(async () => {
    captureFlow();
    const source = flow.steps.find((s) => s.id === $("edge-source").value),
      target = $("edge-target").value,
      outcome = $("edge-outcome").value.trim();
    if (!source || !target || !outcome)
      throw Error("Укажите оба шага и результат");
    const existing = source.transitions.find((p) => p[0] === outcome);
    if (existing) existing[1] = target;
    else source.transitions.push([outcome, target]);
    selected = flow.steps.indexOf(source);
    selectedEdge = [source.id, outcome];
    renderFlow();
    notify("Связь сохранена в черновике");
  });
};
$("edge-delete").onclick = () =>
  act(async () => {
    captureFlow();
    const source = flow.steps.find((s) => s.id === $("edge-source").value);
    if (!source) throw Error("Выберите связь");
    source.transitions = source.transitions.filter(
      ([o, t]) =>
        !(o === $("edge-outcome").value && t === $("edge-target").value),
    );
    selectedEdge = null;
    renderFlow();
    notify("Связь удалена из черновика");
  });
let profileSignature = "";
function renderProfiles() {
  const config = state.profile_config,
    signature = JSON.stringify([config, state.cooldowns]);
  if (signature === profileSignature) return;
  profileSignature = signature;
  const entries = Object.entries(config.profiles || {}),
    root = $("profiles-list");
  root.replaceChildren();
  if (!entries.length)
    root.append(
      el(
        "p",
        "Профилей пока нет. Подключите найденный CLI в карточке выше.",
        "empty",
      ),
    );
  for (const [name, p] of entries) {
    const runner = config.runners[p.runner] || {};
    const card = el("button", undefined, "profile-card");
    card.append(
      el("strong", name),
      el("small", `${runner.adapter || p.runner} · ${p.model}`),
      el(
        "small",
        p.permissions === "workspace-write"
          ? "Может изменять файлы"
          : "Только чтение",
      ),
    );
    card.onclick = () => {
      const form = $("profile-form");
      const values = {
        name,
        adapter: runner.adapter,
        executable: runner.executable,
        arguments: JSON.stringify(runner.arguments || []),
        model: p.model,
        permissions: p.permissions,
        timeout: p.timeout_seconds,
      };
      for (const [key, value] of Object.entries(values))
        form.elements[key].value = value ?? "";
      form.closest("details").open = true;
      form.scrollIntoView({ block: "center", behavior: "smooth" });
      form.elements.name.focus();
    };
    const item = el("div", undefined, "profile-item");
    item.append(card, rotationForm(name, config));
    root.append(item);
  }
}
/* Fallback chain for one profile: which profile takes over after a limit. */
const FAILURE_LABELS = {
  usage_limit: "лимит использования",
  rate_limit: "rate limit",
  authentication: "слетел вход",
  unreachable: "нет сети",
};
function rotationForm(name, config) {
  const rule = config.rotation?.[name];
  const form = el("form", undefined, "rotation-form");
  const others = Object.keys(config.profiles || {}).filter((n) => n !== name);
  const target = el("select");
  target.setAttribute("aria-label", tr("Запасной профиль для") + " " + name);
  target.append(
    option("", tr("не переключаться")),
    ...others.map((n) => option(n, n)),
  );
  target.value = rule?.fallbacks?.[0] || "";
  const cooldown = el("input");
  cooldown.type = "number";
  cooldown.min = 1;
  cooldown.max = 10080;
  cooldown.value = rule?.cooldown_minutes ?? 60;
  cooldown.setAttribute("aria-label", tr("Отдых, минут"));
  const retry = el("input");
  retry.type = "number";
  retry.min = 5;
  retry.max = 3600;
  retry.value = rule?.retry_seconds ?? 20;
  retry.setAttribute("aria-label", tr("Пауза перед повтором, секунд"));
  const triggers = el("div", undefined, "rotation-triggers");
  const on = new Set(
    rule?.on || ["usage_limit", "rate_limit", "authentication"],
  );
  for (const [key, label] of Object.entries(FAILURE_LABELS)) {
    const box = el("label");
    const input = el("input");
    input.type = "checkbox";
    input.value = key;
    input.checked = on.has(key);
    box.append(input, el("span", label));
    triggers.append(box);
  }
  const row = el("div", undefined, "rotation-row");
  const field = (text, input) => {
    const node = el("label", text);
    node.append(input);
    return node;
  };
  row.append(
    field("При сбое переключаться на", target),
    field("Отдых, мин", cooldown),
    field("Повтор через, с", retry),
  );
  const save = el("button", "Сохранить ротацию");
  form.append(row, triggers, save);
  const resting = state.cooldowns?.[name];
  if (resting && resting.until * 1000 > Date.now())
    form.append(
      raw(
        "small",
        tr("Отдыхает до") +
          " " +
          new Date(resting.until * 1000).toLocaleTimeString() +
          " · " +
          (FAILURE_LABELS[resting.reason] || resting.reason),
        "resting",
      ),
    );
  form.onsubmit = (e) => {
    e.preventDefault();
    act(async () => {
      await api("rotation", {
        name,
        fallbacks: target.value ? [target.value] : [],
        on: [...triggers.querySelectorAll("input:checked")].map((i) => i.value),
        cooldown_minutes: Number(cooldown.value),
        retry_seconds: Number(retry.value),
      });
      profileSignature = "";
      notify(
        target.value
          ? tr("Ротация сохранена") + ": " + name + " → " + target.value
          : tr("Ротация для профиля выключена") + ": " + name,
      );
    });
  };
  return form;
}

/* Answering agent questions works like a CLI picker: arrows or digits choose an
 * option, Enter moves on, and the recommended option is marked and preselected. */
const choiceDrafts = persistentMap("choice-drafts");
function questionPanel(d, r, step, id) {
  const panel = el("section", undefined, "question-thread");
  const draftKey = id + ":" + r.active.id;
  const asked = d.questions || [];
  const chosen = choiceDrafts.get(draftKey) || {};
  panel.append(
    el("small", "Команда → вы", "eyebrow"),
    raw("h3", step.prompt || tr("Нужен ваш ответ")),
  );
  const pickers = [];
  asked.forEach((q, index) => {
    const group = el("fieldset", undefined, "picker");
    const legend = el("legend");
    legend.append(
      el("span", String(index + 1) + "/" + asked.length, "picker-index"),
      raw("span", q.text),
    );
    group.append(legend);
    const buttons = [];
    const select = (value, focus) => {
      chosen[q.id] = value;
      choiceDrafts.set(draftKey, chosen);
      buttons.forEach((b) => {
        const on = b.dataset.value === value;
        b.setAttribute("aria-checked", String(on));
        b.tabIndex = on ? 0 : -1;
        if (on && focus) b.focus();
      });
    };
    if (!(q.id in chosen) && q.recommended) chosen[q.id] = q.recommended;
    q.options.forEach((value, i) => {
      const b = el("button", undefined, "option");
      b.type = "button";
      b.setAttribute("role", "radio");
      b.dataset.value = value;
      b.append(el("kbd", String(i + 1)), raw("span", value));
      if (value === q.recommended) b.append(el("em", "рекомендовано"));
      b.onclick = () => select(value, true);
      b.onkeydown = (e) => {
        const at = q.options.indexOf(value);
        if (e.key === "ArrowDown" || e.key === "ArrowRight")
          select(q.options[(at + 1) % q.options.length], true);
        else if (e.key === "ArrowUp" || e.key === "ArrowLeft")
          select(
            q.options[(at - 1 + q.options.length) % q.options.length],
            true,
          );
        else if (/^[1-9]$/.test(e.key) && q.options[Number(e.key) - 1])
          select(q.options[Number(e.key) - 1], true);
        else if (e.key === "Enter") {
          e.preventDefault();
          const next = pickers[index + 1];
          (next
            ? next.querySelector('[aria-checked="true"],.option')
            : note
          ).focus();
          return;
        } else return;
        e.preventDefault();
      };
      buttons.push(b);
    });
    const list = el("div", undefined, "options");
    list.setAttribute("role", "radiogroup");
    list.setAttribute("aria-label", q.text);
    list.append(...buttons);
    group.append(list);
    select(chosen[q.id], false);
    pickers.push(group);
    panel.append(group);
  });
  const legacyChoices = JSON.parse(step.config || "{}").choices;
  const note = el("textarea");
  note.setAttribute("aria-label", "Ваш ответ");
  note.placeholder = asked.length
    ? "Комментарий к выбору (необязательно)"
    : "Напишите ответ команде…";
  note.value = answerDrafts.get(draftKey) || "";
  note.oninput = () => answerDrafts.set(draftKey, note.value);
  if (!asked.length && Array.isArray(legacyChoices)) {
    const list = el("div", undefined, "question-choices");
    legacyChoices
      .filter((choice) => typeof choice === "string")
      .forEach((choice) => {
        const button = raw("button", choice);
        button.type = "button";
        button.onclick = () => {
          note.value = choice;
          answerDrafts.set(draftKey, choice);
          note.focus();
        };
        list.append(button);
      });
    panel.append(list);
  }
  const outcomes = el("select");
  outcomes.setAttribute("aria-label", "Решение");
  step.transitions.forEach(([o]) =>
    outcomes.append(
      option(
        o,
        {
          approved: "Подтвердить",
          answered: "Отправить ответ",
          rejected: "Отклонить",
        }[o] || o,
      ),
    ),
  );
  outcomes.classList.toggle("hidden", step.transitions.length === 1);
  const submit = async (choices) => {
    if (!asked.length && !note.value.trim()) throw Error(tr("Введите ответ"));
    await api("answer", {
      id,
      version: r.version,
      outcome: outcomes.value,
      answer: note.value,
      choices,
    });
    answerDrafts.delete(draftKey);
    choiceDrafts.delete(draftKey);
    document.dispatchEvent(new Event("ffai-answered"));
    await openDetail(id);
  };
  const send = el("button", "Подтвердить ответ", "primary");
  send.onclick = () =>
    act(async () => {
      send.disabled = true;
      try {
        await submit(asked.length ? { ...chosen } : {});
      } finally {
        send.disabled = false;
      }
    });
  const actions = el("div", undefined, "form-actions");
  actions.append(outcomes, send);
  if (asked.length && asked.every((q) => q.recommended)) {
    const accept = el("button", "Принять все рекомендации");
    accept.type = "button";
    accept.onclick = () =>
      act(() =>
        submit(Object.fromEntries(asked.map((q) => [q.id, q.recommended]))),
      );
    actions.append(accept);
  }
  panel.append(note, actions);
  return panel;
}
function autoAnswerSwitch(r, id) {
  const label = el("label", undefined, "switch");
  const input = el("input");
  input.type = "checkbox";
  input.checked = !!r.auto_answer;
  input.disabled = r.status === "accepted";
  input.onchange = () =>
    act(async () => {
      await api(input.checked ? "auto" : "manual", {
        id,
        version: r.version,
        request_id: crypto.randomUUID(),
      });
      notify(
        input.checked
          ? "Вопросы с рекомендациями будут приниматься автоматически. Ответ записывается в историю."
          : "Вопросы снова ждут вашего ответа.",
      );
      await openDetail(id);
    });
  label.append(input, el("span", "Агент выбирает рекомендованные ответы"));
  label.title = tr(
    "Работает, пока очередь запущена и у каждого вопроса есть рекомендация.",
  );
  return label;
}

/* Where an isolated task works and what will be merged back. */
function laneInfo(lane) {
  const box = el("section", undefined, "lane-info");
  const head = el("div", undefined, "lane-head");
  head.append(
    el(
      "strong",
      lane.status === "removed"
        ? "Слито, дорожка убрана"
        : "Изолированная дорожка",
    ),
    raw("code", lane.repos?.[0]?.branch || ""),
  );
  box.append(head, raw("small", lane.root, "mono"));
  const list = el("ul");
  for (const repo of lane.repos || []) {
    const item = el("li");
    item.append(
      raw("code", repo.path),
      el(
        "span",
        repo.fresh
          ? tr("новый репозиторий")
          : tr("от") +
              " " +
              (repo.origin || "HEAD") +
              " @ " +
              repo.base.slice(0, 10),
      ),
    );
    list.append(item);
  }
  box.append(list);
  return box;
}
