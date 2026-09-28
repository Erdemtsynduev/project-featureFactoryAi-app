/* First-run guidance: a checklist whose progress is read from real state, and a
 * short coach-mark tour. Nothing here changes engine state by itself. */
"use strict";
(() => {
  const KEY = "ffai-onboarding";
  const read = () => {
    try {
      return JSON.parse(localStorage.getItem(KEY) || "{}");
    } catch {
      return {};
    }
  };
  const write = (value) => {
    try {
      localStorage.setItem(KEY, JSON.stringify({ ...read(), ...value }));
    } catch {}
  };

  const STEPS = [
    {
      id: "agents",
      title: "Подключите агентов",
      text: "Найдите установленные CLI и подключите профили claude и codex. Вход остаётся в родном CLI.",
      done: () => Object.keys(state.profile_config?.profiles || {}).length > 0,
      action: ["Открыть агентов", () => showTab("settings")],
    },
    {
      id: "project",
      title: "Добавьте проект",
      text: "Проект — это папка с кодом. Подключение не запускает работу.",
      done: () => (state.projects || []).length > 0,
      action: ["Добавить проект", () => $("manage-projects").click()],
    },
    {
      id: "flow",
      title: "Опубликуйте сценарий",
      text: "Сценарий — пайплайн шагов: кто работает, какие проверки и где вы решаете. Публикация фиксирует версию.",
      done: () => (state.definitions || []).length > 0,
      action: ["Открыть сценарии", () => showTab("flows")],
    },
    {
      id: "task",
      title: "Создайте задание",
      text: "Задание создаётся на паузе. Для больших целей начинайте с требования: агент соберёт спецификацию и тикеты.",
      done: () => (state.runs || []).length > 0,
      action: [
        "Новое задание",
        () => {
          showTab("tasks");
          $("new-task").click();
        },
      ],
    },
    {
      id: "run",
      title: "Продолжите задание и запустите очередь",
      text: "Продолжите нужные карточки, затем нажмите «Запустить очередь». Движок сам соблюдает зависимости и лимиты.",
      done: () =>
        state.settings?.running || (state.runs || []).some((r) => !r.paused),
      action: ["Показать очередь", () => $("queue").focus()],
    },
    {
      id: "answer",
      title: "Ответьте на вопрос агента",
      text: "Вопросы приходят с вариантами и рекомендацией. Попробуйте без модели на примере.",
      done: () => read().answered || (state.totals?.accepted || 0) > 0,
      action: ["Пример вопроса", () => $("interactive-demo").click()],
    },
  ];

  const TOUR = [
    [
      "nav",
      "Разделы: задания, сценарии, агенты и лимиты. Кнопка «←» вверху возвращает назад.",
    ],
    [".rail-project", "Проект выбирает папку и фильтрует доску, офис и планы."],
    [
      ".queue-control",
      "Очередь запускается отдельно. Новые задания всегда начинают на паузе.",
    ],
    [
      "#office",
      "Офис: за столами шаги сценария и их модели, документы — задания. Клик открывает задание или шаг.",
    ],
    [
      ".board-toolbar",
      "Доска и Планы: требования превращаются в спецификации и тикеты, фильтры показывают тип и план.",
    ],
  ];

  const panel = document.createElement("section");
  panel.className = "onboarding";
  panel.setAttribute("aria-label", "Первые шаги");
  $("tasks").prepend(panel);

  function render() {
    if (typeof state === "undefined" || !state.settings) return;
    const saved = read();
    const done = STEPS.filter((s) => s.done()).length;
    const hidden = saved.dismissed || done === STEPS.length;
    panel.hidden = hidden && !saved.forced;
    if (panel.hidden) return;
    const next = STEPS.find((s) => !s.done());
    panel.replaceChildren();
    const head = el("div", undefined, "onboarding-head");
    const title = el("div");
    title.append(
      el("h2", "Первые шаги"),
      el("p", `${done} ${tr("из")} ${STEPS.length}`, "hint"),
    );
    const bar = el("span", undefined, "onboarding-bar");
    const fill = el("span");
    fill.style.width = (100 * done) / STEPS.length + "%";
    bar.append(fill);
    const tour = el("button", "Короткий тур");
    tour.type = "button";
    tour.onclick = () => startTour();
    const hide = el("button", "Скрыть");
    hide.type = "button";
    hide.onclick = () => {
      write({ dismissed: true, forced: false });
      render();
    };
    head.append(title, bar, tour, hide);
    const list = el("ol", undefined, "onboarding-steps");
    for (const step of STEPS) {
      const ok = step.done();
      const item = el(
        "li",
        undefined,
        ok ? "done" : step === next ? "current" : "",
      );
      const body = el("div");
      body.append(el("strong", step.title));
      if (step === next) body.append(el("p", step.text));
      item.append(
        el(
          "span",
          ok ? "✓" : String(STEPS.indexOf(step) + 1),
          "onboarding-mark",
        ),
        body,
      );
      if (step === next) {
        const go = el("button", step.action[0], "primary");
        go.type = "button";
        go.onclick = step.action[1];
        item.append(go);
      }
      list.append(item);
    }
    panel.append(head, list);
  }

  let tourIndex = -1;
  const layer = document.createElement("div");
  layer.className = "coach";
  layer.hidden = true;
  document.body.append(layer);
  function startTour() {
    showTab("tasks");
    tourIndex = 0;
    showMark();
  }
  function showMark() {
    const [selector, message] = TOUR[tourIndex] || [];
    const target = selector && document.querySelector(selector);
    if (!target) {
      layer.hidden = true;
      write({ toured: true });
      return;
    }
    target.scrollIntoView({ block: "center", behavior: "instant" });
    const box = target.getBoundingClientRect();
    layer.hidden = false;
    layer.replaceChildren();
    const spot = el("div", undefined, "coach-spot");
    Object.assign(spot.style, {
      left: box.left - 6 + "px",
      top: box.top - 6 + "px",
      width: box.width + 12 + "px",
      height: box.height + 12 + "px",
    });
    const tip = el("div", undefined, "coach-tip");
    tip.setAttribute("role", "dialog");
    tip.append(
      el("small", `${tourIndex + 1} / ${TOUR.length}`, "hint"),
      el("p", message),
    );
    const actions = el("div", undefined, "form-actions");
    const skip = el("button", "Закончить");
    skip.onclick = () => {
      tourIndex = TOUR.length;
      showMark();
    };
    const next = el(
      "button",
      tourIndex === TOUR.length - 1 ? "Готово" : "Дальше",
      "primary",
    );
    next.onclick = () => {
      tourIndex++;
      showMark();
    };
    actions.append(skip, next);
    tip.append(actions);
    const below = box.bottom + 180 < innerHeight;
    Object.assign(tip.style, {
      left: Math.max(12, Math.min(box.left, innerWidth - 340)) + "px",
      top: (below ? box.bottom + 14 : Math.max(12, box.top - 170)) + "px",
    });
    layer.append(spot, tip);
    next.focus();
  }
  layer.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      tourIndex = TOUR.length;
      showMark();
    }
  });

  const help = el("button", "Как пользоваться");
  help.type = "button";
  help.onclick = () => {
    write({ dismissed: false, forced: true });
    showTab("tasks");
    render();
    panel.scrollIntoView({ block: "start", behavior: "smooth" });
  };
  document.querySelector(".rail-foot").prepend(help);

  document.addEventListener("ffai-refreshed", render);
  document.addEventListener("ffai-answered", () => write({ answered: true }));
  document.addEventListener("ffai-ready", () => {
    render();
    if (!read().toured && !(state.runs || []).length) startTour();
  });
})();
