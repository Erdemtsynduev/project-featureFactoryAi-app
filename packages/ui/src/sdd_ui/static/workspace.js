/* UI commands remain versioned; neither drag-and-drop nor messages assign acceptance. */
let currentProject = "",
  detailTab = "discussion";
const messageDrafts = new Map();
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
  }
};
$("project-cancel").onclick = () => $("project-form").classList.add("hidden");
$("project-form").onsubmit = (e) => {
  e.preventDefault();
  act(async () => {
    const doc = Object.fromEntries(new FormData(e.target));
    doc.checks = JSON.parse(doc.checks);
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
function configureDraggable(card, run, preview) {
  card.draggable = !preview && run.status !== "accepted";
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
function configureDropTarget(lane, key, preview) {
  lane.dataset.lane = key;
  lane.ondragover = (e) => {
    if (draggedRun && !preview) {
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
      if (preview) throw Error(tr("Снимки доступны только для чтения."));
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
  const agents = $("agent-status");
  agents.replaceChildren();
  for (const id of ["codex", "claude", "cursor", "opencode"]) {
    const discovery = state.agent_discovery?.find((d) => d.adapter === id),
      configured = Object.entries(state.profile_config.profiles || {}).filter(
        ([, p]) => state.profile_config.runners[p.runner]?.adapter === id,
      );
    const keys = new Set([id, ...configured.map(([name]) => name)]),
      usage = [...keys].reduce(
        (a, k) => {
          const u = state.usage?.by_handler?.[k] || {};
          return {
            calls: a.calls + (u.calls || 0),
            tokens: a.tokens + (u.tokens || 0),
            active: a.active + (u.active || 0),
            unknown: a.unknown + (u.unknown || 0),
          };
        },
        { calls: 0, tokens: 0, active: 0, unknown: 0 },
      );
    const card = el("article", undefined, "agent-card");
    card.append(
      raw("strong", id),
      el("span", configured.length ? "Настроен" : "Не настроен"),
      el(
        "small",
        {
          installed: "Установлен",
          missing: "Не найден",
          ambiguous: "Несколько установок",
          broken: "Ошибка CLI",
        }[discovery?.status] || "Не проверен",
      ),
      el(
        "small",
        id === "codex" && state.usage?.subscription?.windows?.length
          ? "Доступ к квотам подтверждён"
          : "Авторизация: не проверена",
      ),
      el(
        "small",
        id === "codex" && state.usage?.subscription?.windows?.length
          ? "Квоты аккаунта · использовано"
          : "Подписка: нет данных",
      ),
    );
    const subscription = id === "codex" && state.usage?.subscription;
    if (subscription) {
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
          raw(
            "small",
            `${window.bucket} · ${duration} · ${window.used_percent}%`,
          ),
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
      const checked = el("small");
      checked.append(
        el("span", "Проверено"),
        raw(
          "span",
          " · " +
            new Date(subscription.checked_at * 1000).toLocaleString(
              document.documentElement.lang,
            ),
        ),
      );
      card.append(checked);
    }
    if (usage.unknown)
      card.append(el("small", "Часть запусков не сообщила токены"));
    for (const [label, value] of [
      ["Локальные вызовы", usage.calls],
      ["Измеренные токены", usage.tokens],
      ["Активные процессы", usage.active],
    ]) {
      const line = el("small");
      line.append(el("span", label), raw("span", " · " + value));
      card.append(line);
    }
    configured.forEach(([name, p]) =>
      card.append(raw("small", name + " · " + p.model)),
    );
    for (const probe of discovery?.candidates || [])
      card.append(raw("small", probe.version || probe.error));
    agents.append(card);
  }
}
$("discover-agents").onclick = () =>
  act(async () => {
    const b = $("discover-agents");
    b.disabled = true;
    try {
      await api("discover", {});
      workspaceSignature = "";
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
