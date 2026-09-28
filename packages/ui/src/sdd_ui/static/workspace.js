/* UI commands remain versioned; neither drag-and-drop nor messages assign acceptance. */
let currentProject = "",
  detailTab = recall("detail-tab", "discussion");
const messageDrafts = persistentMap("message-drafts");
let draggedRun = null,
  workspaceSignature = "",
  previousRuns = new Map(),
  notificationReady = false;
let notificationKeys = new Set();
try {
  currentProject = localStorage.getItem("ffai-project") || "";
  notificationKeys = new Set(
    JSON.parse(localStorage.getItem("ffai-notifications-seen") || "[]"),
  );
} catch {}
function raw(tag, text, cls) {
  const node = el(tag, text, cls);
  node.dataset.content = "true";
  return node;
}
function matchesProject(run) {
  if (!currentProject) return true;
  const project = state.projects?.find((p) => p.id === currentProject);
  return (
    state.task_metadata?.[run.id]?.project === currentProject ||
    (project && state.locations?.[run.id] === project.workspace)
  );
}
function showTaskDialog() {
  if (!$("task-dialog").open) $("task-dialog").showModal();
}
$("task-dialog").addEventListener("close", () => {
  detailId = null;
  detailRequest++;
});
$("task-dialog").addEventListener("click", (e) => {
  if (e.target === $("task-dialog")) {
    const box = e.target.getBoundingClientRect();
    if (
      e.clientX < box.left ||
      e.clientX > box.right ||
      e.clientY < box.top ||
      e.clientY > box.bottom
    )
      e.target.close();
  }
});
$("language-select").value = window.ffaiPreferences.language;
$("theme-select").value = window.ffaiPreferences.theme;
$("language-select").onchange = (e) => {
  window.ffaiPreferences.set("language", e.target.value);
  $("task-form").elements.language.value = e.target.value;
  $("project-form").elements.language.value = e.target.value;
};
$("theme-select").onchange = (e) =>
  window.ffaiPreferences.set("theme", e.target.value);
$("task-form").elements.language.value = window.ffaiPreferences.language;
$("project-form").elements.language.value = window.ffaiPreferences.language;
$("project-select").onchange = (e) => {
  currentProject = e.target.value;
  try {
    localStorage.setItem("ffai-project", currentProject);
  } catch {}
  const project = state.projects.find((p) => p.id === currentProject);
  if (project) {
    $("task-form").elements.workspace.value = project.workspace;
    $("task-form").elements.language.value = window.ffaiPreferences.language;
    $("live-board").click();
  }
  workspaceSignature = "";
  renderBoard();
  renderOffice();
  renderInbox();
  renderWorkspace();
};
$("manage-projects").onclick = () => {
  const form = $("project-form");
  form.reset();
  form.elements.language.value = window.ffaiPreferences.language;
  form.classList.remove("hidden");
  form.elements.id.focus();
};
$("edit-project").onclick = () => {
  const form = $("project-form"),
    project = state.projects?.find((p) => p.id === currentProject);
  form.classList.toggle("hidden");
  if (project) {
    for (const key of ["id", "name", "workspace", "language"])
      form.elements[key].value = project[key];
    form.elements.checks.value = JSON.stringify(project.checks);
    form.elements.isolation.checked = project.isolation !== false;
    form.elements.auto_resolve.checked = project.auto_resolve !== false;
  }
};
$("project-cancel").onclick = () => $("project-form").classList.add("hidden");
$("project-form").onsubmit = (e) => {
  e.preventDefault();
  act(async () => {
    const doc = Object.fromEntries(new FormData(e.target));
    doc.checks = JSON.parse(doc.checks);
    doc.isolation = e.target.elements.isolation.checked;
    doc.auto_resolve = e.target.elements.auto_resolve.checked;
    const result = await api("project", doc);
    currentProject = result.id;
    workspaceSignature = "";
    $("project-form").classList.add("hidden");
    notify("Проект сохранён");
    await refresh();
    $("project-select").value = currentProject;
    $("project-select").dispatchEvent(new Event("change"));
  });
};
function configureDraggable(card, run) {
  card.draggable = run.status !== "accepted";
  card.dataset.run = run.id;
  card.ondragstart = (e) => {
    draggedRun = run;
    e.dataTransfer.setData("text/plain", run.id);
    e.dataTransfer.effectAllowed = "move";
    document
      .querySelectorAll(".lane")
      .forEach((l) =>
        l.classList.toggle(
          "drop-allowed",
          ["ready", "running"].includes(l.dataset.lane),
        ),
      );
  };
  card.ondragend = () => {
    draggedRun = null;
    document
      .querySelectorAll(".lane")
      .forEach((l) => l.classList.remove("drop-allowed", "drag-over"));
  };
}
function configureDropTarget(lane, key) {
  lane.dataset.lane = key;
  lane.ondragover = (e) => {
    if (draggedRun) {
      e.preventDefault();
      e.dataTransfer.dropEffect = ["ready", "running"].includes(key)
        ? "move"
        : "none";
      lane.classList.add("drag-over");
    }
  };
  lane.ondragleave = () => lane.classList.remove("drag-over");
  lane.ondrop = (e) => {
    e.preventDefault();
    lane.classList.remove("drag-over");
    const run = draggedRun;
    draggedRun = null;
    document
      .querySelectorAll(".lane")
      .forEach((l) => l.classList.remove("drop-allowed"));
    if (!run) return;
    act(async () => {
      if (!["ready", "running"].includes(key))
        throw Error(
          tr(
            "Перенос в «Готово» недоступен: результат принимается после проверок.",
          ),
        );
      if (run.active && stepFor(run)?.kind === "human")
        throw Error(tr("Сначала ответьте на вопрос в карточке."));
      if (key === "running" && ["blocked", "waiting"].includes(run.status))
        throw Error(
          tr(
            "Откройте карточку и устраните причину блокировки перед продолжением.",
          ),
        );
      await api(key === "ready" ? "pause" : "resume", {
        id: run.id,
        version: run.version,
        request_id: crypto.randomUUID(),
      });
      notify(
        key === "ready"
          ? "Задание поставлено на паузу"
          : "Задание готово к запуску. Очередь включается отдельно.",
      );
    });
  };
}
$("notifications").onclick = async () => {
  if (!("Notification" in window)) {
    notify("Браузер не поддерживает уведомления", true);
    return;
  }
  const permission = await Notification.requestPermission();
  try {
    localStorage.setItem(
      "ffai-notifications",
      permission === "granted" ? "enabled" : "disabled",
    );
  } catch {}
  notify(
    permission === "granted"
      ? "Уведомления включены"
      : "Уведомления заблокированы браузером",
    permission !== "granted",
  );
};
function updateNotifications() {
  for (const run of state.runs) {
    const before = previousRuns.get(run.id);
    if (before && before.step !== run.step) {
      $("office").classList.remove("mail-transfer");
      void $("office").offsetWidth;
      $("office").classList.add("mail-transfer");
    }
    const waiting = run.active && stepFor(run)?.kind === "human";
    const key =
      run.id +
      ":" +
      (waiting
        ? run.active.id
        : run.status === "blocked"
          ? "blocked:" + run.reason
          : run.status === "accepted"
            ? "accepted"
            : "");
    if (
      (waiting || run.status === "blocked" || run.status === "accepted") &&
      !notificationKeys.has(key)
    ) {
      notificationKeys.add(key);
      let enabled = false;
      try {
        enabled = localStorage.getItem("ffai-notifications") === "enabled";
      } catch {}
      if (
        notificationReady &&
        enabled &&
        "Notification" in window &&
        Notification.permission === "granted"
      ) {
        const n = new Notification(
          tr(
            waiting
              ? "Нужен ваш ответ"
              : run.status === "accepted"
                ? "Готово"
                : "Нужны вы",
          ),
          { body: state.task_metadata?.[run.id]?.title || run.id, tag: key },
        );
        n.onclick = () => {
          window.focus();
          showTab("tasks");
          act(() => openDetail(run.id));
          n.close();
        };
      }
    }
    previousRuns.set(run.id, { step: run.step, status: run.status });
  }
  notificationReady = true;
  try {
    localStorage.setItem(
      "ffai-notifications-seen",
      JSON.stringify([...notificationKeys].slice(-256)),
    );
  } catch {}
}
function renderWorkspace() {
  updateNotifications();
  renderAgents();
  const signature = JSON.stringify([
    state.projects,
    state.usage,
    state.agent_discovery,
    state.totals,
    state.settings,
    currentProject,
    state.active_processes,
  ]);
  if (signature === workspaceSignature) return;
  workspaceSignature = signature;
  options("project-select", [
    option("", "Все проекты"),
    ...(state.projects || []).map((p) => option(p.id, p.name)),
  ]);
  if (currentProject && !state.projects.some((p) => p.id === currentProject))
    currentProject = "";
  $("project-select").value = currentProject;
  const project = state.projects.find((p) => p.id === currentProject);
  $("project-path").textContent = project?.workspace || "";
  $("edit-project").disabled = !project;
  if (project && !$("task-form").elements.workspace.value)
    $("task-form").elements.workspace.value = project.workspace;
  const charts = $("budget-charts");
  charts.replaceChildren();
  for (const [label, value, max] of [
    ["Общий лимит", state.totals.calls, state.settings.max_calls],
    [
      "Планирование",
      state.totals.planning_calls,
      state.settings.max_planning_calls,
    ],
  ]) {
    const item = el("section", undefined, "budget-item"),
      progress = el("progress");
    progress.max = Math.max(1, max);
    progress.value = value;
    progress.setAttribute("aria-label", label);
    item.append(el("strong", label), el("span", `${value} / ${max}`), progress);
    charts.append(item);
  }
  const history = el("section", undefined, "usage-history");
  history.append(el("strong", "Запуски шагов по дням"));
  const daily = Object.entries(state.usage?.daily_dispatches || {});
  if (!daily.length) history.append(el("p", "Данных пока нет", "hint"));
  else {
    const max = Math.max(1, ...daily.map(([, n]) => n)),
      svg = svgEl("svg", {
        viewBox: `0 0 ${daily.length * 48} 90`,
        role: "img",
        "aria-label": "Запуски шагов по дням",
      });
    daily.forEach(([day, n], i) => {
      const bar = svgEl("rect", {
        x: i * 48 + 8,
        y: 65 - (n / max) * 50,
        width: 24,
        height: (n / max) * 50,
        rx: 3,
        class: "usage-bar",
      });
      bar.append(svgEl("title", {}, `${day}: ${n}`));
      svg.append(
        bar,
        svgEl(
          "text",
          {
            x: i * 48 + 20,
            y: 82,
            "text-anchor": "middle",
            class: "usage-label",
          },
          day.slice(5),
        ),
      );
    });
    history.append(svg);
  }
  charts.append(history);
}
const modelCache = new Map();
let agentsSignature = "";
function pill(text, tone) {
  return el("span", text, "pill" + (tone ? " " + tone : ""));
}
function agentUsage(id, configured) {
  const keys = new Set([id, ...configured.map(([name]) => name)]);
  return [...keys].reduce(
    (a, k) => {
      const u = state.usage?.by_handler?.[k] || {};
      return {
        calls: a.calls + (u.calls || 0),
        tokens: a.tokens + (u.tokens || 0),
        active: a.active + (u.active || 0),
        unknown: a.unknown + (u.unknown || 0),
        usd: a.usd + (u.usd || 0),
      };
    },
    { calls: 0, tokens: 0, active: 0, unknown: 0, usd: 0 },
  );
}
function codexQuota(card) {
  const subscription = state.usage?.subscription;
  if (!subscription) return;
  for (const window of subscription.windows || []) {
    const meter = el("progress");
    meter.max = 100;
    meter.value = window.used_percent;
    meter.setAttribute("aria-label", window.bucket + " " + window.window);
    const minutes = window.duration_minutes;
    const unit =
      minutes && minutes % 1440 === 0
        ? "day"
        : minutes && minutes % 60 === 0
          ? "hour"
          : "minute";
    const duration =
      minutes == null
        ? "?"
        : new Intl.NumberFormat(document.documentElement.lang, {
            style: "unit",
            unit,
            unitDisplay: "long",
          }).format(
            minutes / (unit === "day" ? 1440 : unit === "hour" ? 60 : 1),
          );
    card.append(
      raw("small", `${window.bucket} · ${duration} · ${window.used_percent}%`),
      meter,
    );
    if (window.resets_at) {
      const line = el("small");
      line.append(
        el("span", "Сброс лимита"),
        raw(
          "span",
          " · " +
            new Date(window.resets_at * 1000).toLocaleString(
              document.documentElement.lang,
            ),
        ),
      );
      card.append(line);
    }
  }
  if (subscription.status === "unavailable")
    card.append(el("small", "Квоты недоступны"));
}
function connectForm(id, discovery, configured) {
  const form = el("form", undefined, "connect-form");
  const field = (text, input, wide) => {
    const node = el("label", text);
    if (wide) node.className = "wide";
    node.append(input);
    return node;
  };
  const name = el("input");
  name.value = configured[0]?.[0] || id;
  name.required = true;
  const model = el("input");
  model.required = true;
  model.placeholder = "ID модели";
  model.value = configured[0]?.[1].model || "";
  const list = el("datalist");
  list.id = "models-" + id;
  model.setAttribute("list", list.id);
  const loadModels = async () => {
    if (!modelCache.has(id)) {
      try {
        modelCache.set(id, (await api("models", { adapter: id })).models);
      } catch {
        modelCache.set(id, discovery.suggested_models || []);
      }
    }
    const models = modelCache.get(id);
    list.replaceChildren(...models.map((m) => option(m, m)));
    if (!model.value && models.length) model.value = models[0];
  };
  loadModels();
  const rights = el("select");
  rights.append(
    option("workspace-write", "Может изменять файлы"),
    option("read-only", "Только чтение"),
  );
  rights.value = configured[0]?.[1].permissions || "workspace-write";
  const submit = el(
    "button",
    configured.length ? "Обновить профиль" : "Подключить",
    "primary wide",
  );
  form.append(
    field("Профиль", name),
    field("Модель", model),
    list,
    field("Права", rights, true),
    submit,
  );
  form.onsubmit = (e) => {
    e.preventDefault();
    act(async () => {
      await api("connect", {
        adapter: id,
        argv: discovery.selected,
        name: name.value.trim(),
        model: model.value.trim(),
        permissions: rights.value,
      });
      agentsSignature = "";
      notify(
        "Профиль «" + name.value.trim() + "» подключён. Вызовов моделей: 0.",
      );
    });
  };
  return form;
}
function renderAgents() {
  const signature = JSON.stringify([
    state.agent_discovery,
    state.profile_config,
    state.usage,
  ]);
  if (signature === agentsSignature) return;
  agentsSignature = signature;
  const agents = $("agent-status");
  agents.replaceChildren();
  const found = state.agent_discovery || [];
  if (!found.length)
    agents.append(
      el(
        "p",
        "Нажмите «Найти CLI и проверить вход». Запускаются только команды версии и статуса входа, модель не вызывается.",
        "empty",
      ),
    );
  for (const discovery of found) {
    const id = discovery.adapter;
    const configured = Object.entries(
      state.profile_config.profiles || {},
    ).filter(([, p]) => state.profile_config.runners[p.runner]?.adapter === id);
    const card = el("article", undefined, "agent-card");
    const head = el("div", undefined, "agent-head");
    head.append(raw("strong", id));
    head.append(
      discovery.status === "installed"
        ? pill("Установлен", "good")
        : discovery.status === "missing"
          ? pill("Не найден")
          : discovery.status === "ambiguous"
            ? pill("Несколько установок", "warn")
            : pill("Ошибка CLI", "bad"),
    );
    if (discovery.status === "installed")
      head.append(
        discovery.authentication === "authenticated"
          ? pill("Вход выполнен", "good")
          : discovery.authentication === "not_authenticated"
            ? pill("Нужен вход", "bad")
            : pill("Вход неизвестен"),
      );
    card.append(head);
    const chosen = discovery.candidates.find(
      (c) => discovery.selected && c.argv.join() === discovery.selected.join(),
    );
    if (chosen?.version) card.append(raw("small", chosen.version));
    if (discovery.authentication_detail)
      card.append(raw("small", discovery.authentication_detail));
    if (discovery.selected)
      card.append(raw("small", discovery.selected[0], "mono"));
    if (discovery.candidates.length > 1)
      card.append(
        el(
          "small",
          "Установок найдено: " +
            discovery.candidates.length +
            (discovery.status === "installed" ? " · выбрана новейшая" : ""),
        ),
      );
    for (const [name, p] of configured)
      card.append(
        raw(
          "small",
          name +
            " → " +
            p.model +
            (p.permissions === "workspace-write" ? " · запись" : " · чтение"),
          "mono",
        ),
      );
    const usage = agentUsage(id, configured);
    const stats = el("div", undefined, "agent-stats");
    for (const [label, value] of [
      ["вызовы", usage.calls],
      ["токены", usage.tokens.toLocaleString()],
      ["активно", usage.active],
      ["≈ API", money(usage.usd)],
    ]) {
      const item = el("span");
      item.append(raw("b", String(value)), el("span", " " + label));
      stats.append(item);
    }
    card.append(stats);
    if (usage.unknown)
      card.append(el("small", "Часть запусков не сообщила токены"));
    if (id === "codex") codexQuota(card);
    if (discovery.selected) card.append(connectForm(id, discovery, configured));
    agents.append(card);
  }
}
$("discover-agents").onclick = () =>
  act(async () => {
    const b = $("discover-agents");
    b.disabled = true;
    notify("Проверяю установленные CLI и статус входа…");
    try {
      const found = await api("discover", {});
      modelCache.clear();
      agentsSignature = "";
      const ready = found.filter((d) => d.authentication === "authenticated");
      notify(
        tr("Проверка завершена") +
          ": " +
          found.filter((d) => d.status === "installed").length +
          " " +
          tr("установлено") +
          ", " +
          ready.length +
          " " +
          tr("с выполненным входом") +
          ".",
      );
    } finally {
      b.disabled = false;
    }
  });
$("subscription-status").onclick = () =>
  act(async () => {
    const button = $("subscription-status");
    button.disabled = true;
    try {
      await api("subscription", {});
      workspaceSignature = "";
    } finally {
      button.disabled = false;
    }
  });
$("interactive-demo").onclick = () =>
  act(async () => {
    const run = await api("interactive-demo", {
      language: window.ffaiPreferences.language,
    });
    await refresh();
    $("live-board").click();
    detailTab = "discussion";
    await openDetail(run.id);
    notify("Модель не вызывалась");
  });
