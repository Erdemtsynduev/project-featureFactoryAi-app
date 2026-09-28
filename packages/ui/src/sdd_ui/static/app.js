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
async function refresh() {
  try {
    state = await api("state");
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
    $("last").textContent = state.last_transition
      ? new Date(state.last_transition * 1000).toLocaleString()
      : "—";
    if (state.error) notify(state.error, true);
    if (!boardInitialized) {
      boardInitialized = true;
      if (!state.runs.length && state.preview?.items.length)
        $("preview-board").click();
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
};
const roles = {
  spec: "Аналитик",
  tickets: "Планировщик",
  implement: "Разработчик",
  checks: "Тестировщик",
  review: "Ревьюер",
  diagnose: "Диагност",
  repair: "Разработчик",
  reconcile: "Диагност",
  interview: "Вы",
  approve: "Вы",
};
let boardInitialized = false;
let boardMode = "live",
  boardSignature = "",
  officeSignature = "",
  inboxSignature = "";
let detailId = null,
  detailVersion = null,
  detailRequest = 0;
const answerDrafts = new Map();
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
function renderBoard() {
  const preview = boardMode === "preview",
    snapshot = state.preview || { items: [] };
  const all = preview
    ? snapshot.items
    : state.runs.filter(matchesProject).map((r) => ({
        ...r,
        title: state.task_metadata?.[r.id]?.title || r.id,
      }));
  const query = $("task-search").value.toLowerCase();
  const plan = $("plan-filter").value;
  const items = all.filter(
    (r) =>
      (!preview || !plan || r.plan === plan) &&
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
    state.definitions,
  ]);
  if (signature === boardSignature) return;
  boardSignature = signature;
  $("plan-filter").disabled = !preview;
  options("plan-filter", [
    option("", "Все планы"),
    ...[...new Set(snapshot.items.map((r) => r.plan))]
      .sort()
      .map((p) => option(p, "План " + p)),
  ]);
  $("snapshot-info").classList.toggle("hidden", !preview);
  $("snapshot-info").textContent = snapshot.captured_at
    ? `Снимок от ${new Date(snapshot.captured_at).toLocaleString()} · ${snapshot.items.length} карточек · только просмотр. Статусы взяты из исходного оркестратора; это не результаты нового движка.`
    : "Снимок ещё не подключён. Создайте его командой из инструкции docs/UI.md; действующая очередь не переносится.";
  $("board").replaceChildren();
  for (const [key, title] of [
    ["ready", "К выполнению"],
    ["running", "В работе"],
    ["blocked", "Нужны вы"],
    ["accepted", "Готово"],
  ]) {
    const runs = items.filter((r) => laneFor(r) === key);
    const lane = el("section", undefined, "lane lane-" + key);
    configureDropTarget(lane, key, preview);
    const heading = el("h2", title);
    heading.append(el("span", runs.length, "lane-count"));
    lane.append(heading);
    const visible = runs.slice(0, 40);
    for (const r of visible) {
      const c = el("button", undefined, "card");
      configureDraggable(c, r, preview);
      c.append(
        el(
          "small",
          preview
            ? `План ${r.plan} · ${r.kind === "ticket" ? "Тикет" : "Требование"}`
            : roles[r.step] || r.step,
          "card-kicker",
        ),
        raw("strong", r.title || r.id),
        el(
          "small",
          preview
            ? r.id
            : (labels[r.step] || r.step) + (r.paused ? " · пауза" : ""),
        ),
      );
      if (!preview)
        c.append(
          el("small", r.calls + " вызовов · планирование " + r.planning_calls),
        );
      if (r.reason && r.status !== "accepted")
        c.append(raw("small", r.reason, "card-reason"));
      if (r.dependencies?.length)
        c.append(el("small", "Зависит от: " + r.dependencies.join(", ")));
      c.onclick = () =>
        preview ? openPreview(r) : act(() => openDetail(r.id));
      lane.append(c);
    }
    if (runs.length > 40)
      lane.append(
        el(
          "p",
          `Ещё ${runs.length - 40}. Выберите план или уточните поиск.`,
          "hint",
        ),
      );
    if (!runs.length)
      lane.append(
        el(
          "div",
          query || plan
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
function openPreview(r) {
  detailId = null;
  showTaskDialog();
  const root = $("detail");
  root.classList.remove("hidden");
  root.replaceChildren(
    el("small", "СНИМОК · ТОЛЬКО ПРОСМОТР", "eyebrow"),
    el("h2", r.id),
    raw("h3", r.title),
    raw("p", r.context),
    el("p", "Исходный статус: " + r.status),
    raw("p", r.reason),
    el("p", "Источник: " + r.path),
    el("p", "Зависимости: " + (r.dependencies?.join(", ") || "нет")),
  );
  appendClose(root);
  root.scrollIntoView({ block: "start", behavior: "smooth" });
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
function renderOffice() {
  const signature = JSON.stringify([
    state.runs.filter(matchesProject),
    state.settings.running,
  ]);
  if (signature === officeSignature) return;
  officeSignature = signature;
  $("office").replaceChildren();
  for (const [id, name] of [
    ["spec", "Аналитик"],
    ["tickets", "Планировщик"],
    ["implement", "Разработчик"],
    ["checks", "Тестировщик"],
    ["review", "Ревьюер"],
    ["interview", "Вы"],
  ]) {
    const working = state.runs
      .filter(matchesProject)
      .filter(
        (r) =>
          r.active &&
          (r.step === id ||
            (id === "implement" && r.step === "repair") ||
            (id === "interview" && stepFor(r)?.kind === "human")),
      );
    const person = el(
      "button",
      undefined,
      "workstation" + (working.length ? " busy" : " idle"),
    );
    const scene = el("span", undefined, "pixel-scene");
    scene.setAttribute("aria-hidden", "true");
    scene.append(
      el("span", undefined, "pixel-person"),
      el("span", undefined, "pixel-desk"),
      el("span", undefined, "pixel-cup"),
      el("span", undefined, "pixel-phone"),
      el("span", undefined, "pixel-plant"),
      el("span", undefined, "pixel-mail"),
    );
    person.append(
      scene,
      el("strong", name),
      el(
        "small",
        working.length
          ? `${working.length} ${id === "interview" ? "ждут ответа" : "в работе"}`
          : "Свободен",
      ),
    );
    person.onclick = () =>
      working.length
        ? act(() => openDetail(working[0].id))
        : act(() => openMainFlow(id));
    $("office").append(person);
  }
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
  ["preview-board", "preview"],
])
  $(id).onclick = () => {
    boardMode = mode;
    document
      .querySelectorAll(".segmented button")
      .forEach((b) => b.classList.toggle("active", b.id === id));
    renderBoard();
  };
$("task-search").oninput = renderBoard;
$("plan-filter").onchange = renderBoard;
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
  const reload = el("button", "Обновить");
  reload.onclick = () => act(() => openDetail(id));
  actions.append(reload);
  root.append(actions);
  const tabs = el("div", undefined, "detail-tabs"),
    discussion = el("section"),
    details = el("section"),
    events = el("section");
  const panes = { discussion, details, events };
  const choose = (key) => {
    detailTab = key;
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
    const question = el("section", undefined, "question-thread");
    question.append(
      el("small", "КОМАНДА → ВЫ", "eyebrow"),
      raw("h3", step.prompt || tr("Нужен ваш ответ")),
    );
    const answer = el("textarea"),
      draftKey = id + ":" + r.active.id;
    answer.setAttribute("aria-label", "Ваш ответ");
    answer.placeholder = "Напишите ответ команде…";
    answer.value = answerDrafts.get(draftKey) || "";
    answer.oninput = () => answerDrafts.set(draftKey, answer.value);
    const choices = JSON.parse(step.config || "{}").choices;
    if (Array.isArray(choices)) {
      const list = el("div", undefined, "question-choices");
      choices
        .filter((choice) => typeof choice === "string")
        .forEach((choice) => {
          const button = raw("button", choice);
          button.onclick = () => {
            answer.value = choice;
            answerDrafts.set(draftKey, choice);
            answer.focus();
          };
          list.append(button);
        });
      question.append(list);
    }
    const outcomes = el("select");
    outcomes.setAttribute("aria-label", "Решение");
    step.transitions.forEach(([o, t]) =>
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
    const send = el("button", "Подтвердить ответ", "primary");
    send.onclick = () =>
      act(async () => {
        if (!answer.value.trim()) throw Error(tr("Введите ответ"));
        send.disabled = true;
        try {
          await api("answer", {
            id,
            version: r.version,
            outcome: outcomes.value,
            answer: answer.value,
          });
          answerDrafts.delete(draftKey);
        } finally {
          send.disabled = false;
        }
        await openDetail(id);
      });
    question.append(answer, outcomes, send);
    discussion.append(question);
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
      positions[flow.id + ":" + doc.id] = position(old, selected);
      delete positions[flow.id + ":" + old.id];
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
    try {
      const saved = localStorage.getItem("ffai-flow-draft");
      if (saved) {
        const draft = JSON.parse(saved);
        flow = draft.flow;
        positions = draft.positions || {};
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
let positions = {},
  connecting = null,
  graphZoom = 1;
const ns = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs = {}, text) {
  const n = document.createElementNS(ns, tag);
  Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v));
  if (text !== undefined) n.textContent = text;
  return n;
}
function position(s, i) {
  const defaults = {
    spec: [40, 65],
    tickets: [370, 65],
    implement: [700, 65],
    interview: [40, 310],
    diagnose: [370, 310],
    checks: [700, 310],
    accepted: [40, 555],
    reconcile: [40, 800],
    repair: [370, 555],
    review: [700, 555],
  };
  return (
    positions[flow.id + ":" + s.id] ||
    defaults[s.id] || [40 + (i % 3) * 330, 800 + Math.floor(i / 3) * 245]
  );
}
function renderGraph() {
  const root = $("graph");
  root.replaceChildren();
  const height = Math.max(
    735,
    ...flow.steps.map((s, i) => position(s, i)[1] + 160),
  );
  const svg = svgEl("svg", {
    viewBox: `0 0 1010 ${height}`,
    width: 1010 * graphZoom,
    height: height * graphZoom,
    "aria-label": "Шаги и связи main flow",
  });
  let pan = null;
  svg.onpointerdown = (e) => {
    if (e.target !== svg) return;
    pan = [e.clientX, e.clientY, root.scrollLeft, root.scrollTop];
    svg.setPointerCapture(e.pointerId);
  };
  svg.onpointermove = (e) => {
    if (pan) {
      root.scrollLeft = pan[2] - (e.clientX - pan[0]);
      root.scrollTop = pan[3] - (e.clientY - pan[1]);
    }
  };
  svg.onpointerup = svg.onpointercancel = () => {
    pan = null;
  };
  const defs = svgEl("defs"),
    marker = svgEl("marker", {
      id: "arrow",
      viewBox: "0 0 10 10",
      refX: 9,
      refY: 5,
      markerWidth: 6,
      markerHeight: 6,
      orient: "auto-start-reverse",
    });
  marker.append(
    svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", class: "arrow-tip" }),
  );
  defs.append(marker);
  svg.append(defs);
  flow.steps.forEach((s, i) => {
    const [x, y] = position(s, i);
    s.transitions.forEach(([outcome, target], edgeIndex) => {
      const targetIndex = flow.steps.findIndex((t) => t.id === target);
      if (targetIndex < 0) return;
      const [tx, ty] = position(flow.steps[targetIndex], targetIndex);
      let sx = x + 230,
        sy = y + 48,
        ex = tx,
        ey = ty + 48;
      let d;
      if (tx === x && ty > y) {
        sx = x + 115;
        sy = y + 96;
        ex = tx + 115;
        ey = ty;
        d = `M${sx},${sy} C${sx},${sy + 65} ${ex},${ey - 65} ${ex},${ey}`;
      } else if (tx < x || (tx === x && ty <= y)) {
        const bend = Math.max(y, ty) + 145 + edgeIndex * 22;
        d = `M${sx},${sy} C${sx + 70},${bend} ${ex - 70},${bend} ${ex},${ey}`;
      } else d = `M${sx},${sy} C${sx + 60},${sy} ${ex - 60},${ey} ${ex},${ey}`;
      const path = svgEl("path", {
        d,
        class: "flow-edge" + (outcome === "failed" ? " retry-edge" : ""),
        "marker-end": "url(#arrow)",
        tabindex: 0,
        role: "button",
        "aria-label": `${s.id}: ${outcome} → ${target}`,
      });
      const choose = () => {
        $("edge-source").value = s.id;
        $("edge-target").value = target;
        $("edge-outcome").value = outcome;
        notify(
          `Связь: ${s.id} → ${target}. Её можно изменить или удалить ниже.`,
        );
      };
      path.onclick = choose;
      path.onkeydown = (e) => {
        if (e.key === "Enter") choose();
      };
      svg.append(path);
      svg.append(
        svgEl("text", { x: sx + 8, y: sy - 9, class: "edge-label" }, outcome),
      );
    });
  });
  flow.steps.forEach((s, i) => {
    const [x, y] = position(s, i);
    const group = svgEl("g", {
      transform: `translate(${x},${y})`,
      class: "flow-node" + (i === selected ? " selected" : ""),
      tabindex: 0,
      role: "button",
      "aria-label": `${labels[s.id] || s.id} · ${s.id}`,
    });
    group.append(
      svgEl("rect", { width: 230, height: 96, rx: 12, class: "node-body" }),
      svgEl(
        "text",
        { x: 18, y: 25, class: "node-role" },
        roles[s.id] || s.kind,
      ),
      svgEl(
        "text",
        { x: 18, y: 49, class: "node-title" },
        (labels[s.id] || s.id).slice(0, 25),
      ),
      svgEl(
        "text",
        { x: 18, y: 73, class: "node-meta" },
        (s.kind === "human"
          ? "Ожидание ответа"
          : s.handler || s.profile || s.kind
        ).slice(0, 28),
      ),
    );
    if (flow.entry === s.id)
      group.append(
        svgEl("text", { x: 172, y: 24, class: "node-role" }, "СТАРТ"),
      );
    const choose = () => {
      captureFlow();
      selected = i;
      renderFlow();
    };
    group.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        choose();
      }
    };
    let drag = null,
      moved = false;
    group.onpointerdown = (e) => {
      if (e.target.classList.contains("port")) return;
      drag = [e.clientX, e.clientY, x, y];
      moved = false;
      group.setPointerCapture(e.pointerId);
    };
    group.onpointermove = (e) => {
      if (!drag) return;
      const scale = 1010 / svg.getBoundingClientRect().width;
      const dx = (e.clientX - drag[0]) * scale,
        dy = (e.clientY - drag[1]) * scale;
      if (Math.abs(dx) + Math.abs(dy) > 4) moved = true;
      if (moved) {
        positions[flow.id + ":" + s.id] = [
          Math.max(5, Math.min(770, drag[2] + dx)),
          Math.max(5, Math.min(height - 100, drag[3] + dy)),
        ];
        group.setAttribute(
          "transform",
          `translate(${positions[flow.id + ":" + s.id].join(",")})`,
        );
      }
    };
    group.onpointerup = () => {
      if (!drag) return;
      drag = null;
      if (moved) renderGraph();
      else choose();
    };
    group.onpointercancel = () => {
      drag = null;
      renderGraph();
    };
    for (const [kind, cx] of [
      ["input", 0],
      ["output", 230],
    ]) {
      const port = svgEl("circle", {
        cx,
        cy: 48,
        r: 7,
        class: "port",
        tabindex: 0,
        role: "button",
        "aria-label": (kind === "output" ? "Выход " : "Вход ") + s.id,
      });
      const connect = () => {
        if (kind === "output") {
          connecting = s.id;
          $("edge-source").value = s.id;
          notify(
            "Теперь выберите вход следующего шага. Результат связи задаётся в поле «Результат».",
          );
        } else if (connecting) {
          $("edge-target").value = s.id;
          $("edge-source").value = connecting;
          connecting = null;
          $("edge-form").requestSubmit();
        }
      };
      port.onpointerdown = (e) => e.stopPropagation();
      port.onclick = (e) => {
        e.stopPropagation();
        connect();
      };
      port.onkeydown = (e) => {
        if (e.key === "Enter") {
          e.stopPropagation();
          connect();
        }
      };
      group.append(port);
    }
    svg.append(group);
  });
  root.append(svg);
}
$("graph-reset").onclick = () => {
  positions = {};
  renderGraph();
};
$("zoom-in").onclick = () => {
  graphZoom = Math.min(1.8, graphZoom + 0.15);
  renderGraph();
};
$("zoom-out").onclick = () => {
  graphZoom = Math.max(0.4, graphZoom - 0.15);
  renderGraph();
};
$("graph-fit").onclick = () => {
  const height = Math.max(
    735,
    ...flow.steps.map((s, i) => position(s, i)[1] + 160),
  );
  graphZoom = Math.max(
    0.25,
    Math.min(
      1,
      ($("graph").clientWidth - 20) / 1010,
      ($("graph").clientHeight - 20) / height,
    ),
  );
  renderGraph();
};
$("draft-save").onclick = () =>
  act(async () => {
    localStorage.setItem(
      "ffai-flow-draft",
      JSON.stringify({ flow: captureFlow(), positions }),
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
    renderFlow();
    notify("Связь удалена из черновика");
  });
let profileSignature = "";
function renderProfiles() {
  const config = state.profile_config,
    signature = JSON.stringify(config);
  if (signature === profileSignature) return;
  profileSignature = signature;
  const entries = Object.entries(config.profiles || {}),
    root = $("profiles-list");
  root.replaceChildren();
  if (!entries.length)
    root.append(
      el(
        "p",
        "Добавьте исполнителя справа, затем выберите его профиль в свойствах шага.",
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
      form.scrollIntoView({ block: "center", behavior: "smooth" });
      form.elements.name.focus();
    };
    root.append(card);
  }
}
