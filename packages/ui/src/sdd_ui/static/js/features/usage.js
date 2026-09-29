/* Usage and limits: what was spent, what the queue may still spend, and what
 * the providers report about account quotas.
 *
 * Numbers come from recorded results: each agent CLI reports its tokens per
 * call, and they update as soon as a step finishes. Cost is the public API
 * price of those tokens, not a subscription bill. Account quotas are read only
 * from Codex (its app-server) on request or when this page is open. */

import * as api from "../core/api.js";
import { h, memo, replace, svg } from "../core/dom.js";
import {
  formatNumber,
  formatTime,
  language,
  money,
  relativeTime,
  t,
} from "../core/i18n.js";
import { refresh, runs, store } from "../core/store.js";
import { attempt, toastError } from "../ui/toast.js";
import { registerView } from "./shell.js";

let root = null;
let nodes = {};
const renderUsage = memo();

function meterBar(label, value, max, hint) {
  const ratio = max ? Math.min(1, value / max) : 0;
  return h(
    "div",
    { class: "meter" + (ratio >= 0.9 ? " is-high" : "") },
    h(
      "div",
      { class: "meter-head" },
      h("strong", {}, label),
      h("span", { class: "mono" }, `${value} / ${max}`),
    ),
    h(
      "div",
      {
        class: "meter-track",
        role: "progressbar",
        "aria-label": label,
        "aria-valuenow": value,
        "aria-valuemax": max,
      },
      h("span", { style: { width: Math.round(ratio * 100) + "%" } }),
    ),
    hint ? h("small", { class: "hint" }, hint) : null,
  );
}

function budgetPanel() {
  const { totals, settings } = store.state;
  // A budget smaller than the open work stops the queue midway; say it upfront.
  const open = runs().filter((r) => r.status !== "accepted").length;
  // The cap is optional: subscriptions are paced by their windows, not by calls.
  const capped = settings.max_calls != null;
  const small = capped && open > settings.max_calls - totals.calls;
  const calls = h("input", {
    name: "max_calls",
    type: "number",
    min: "0",
    placeholder: "∞",
    value: settings.max_calls ?? "",
  });
  const planning = h("input", {
    name: "max_planning_calls",
    type: "number",
    min: "0",
    placeholder: "∞",
    value: settings.max_planning_calls ?? "",
  });
  const cap = (input) => (input.value === "" ? null : Number(input.value));
  const revive = h("input", {
    type: "checkbox",
    name: "revive",
    checked: settings.revive !== false,
  });
  const save = h(
    "button",
    { type: "submit", class: "primary" },
    t("usage.saveLimits"),
  );
  return h(
    "section",
    { class: "panel" },
    h("h2", {}, t("usage.budgetTitle")),
    small
      ? h(
          "p",
          { class: "attention tone-waiting" },
          t("usage.budgetSmall", { open, max: settings.max_calls }),
        )
      : null,
    capped ? meterBar(t("usage.allCalls"), totals.calls, settings.max_calls) : null,
    settings.max_planning_calls != null
      ? meterBar(
          t("usage.planningCalls"),
          totals.planning_calls,
          settings.max_planning_calls,
        )
      : null,
    h("p", { class: "hint" }, t("usage.capHint")),
    h(
      "form",
      {
        id: "budget-form",
        class: "form-grid",
        onsubmit: async (event) => {
          event.preventDefault();
          save.disabled = true;
          await attempt(async () => {
            await api.post("budget", {
              max_calls: cap(calls),
              max_planning_calls: cap(planning),
              revive: revive.checked,
            });
            await refresh();
          }, t("usage.limitsSaved"));
          save.disabled = false;
        },
      },
      h("label", { class: "field" }, h("span", {}, t("usage.allCalls")), calls),
      h(
        "label",
        { class: "field" },
        h("span", {}, t("usage.planningCalls")),
        planning,
      ),
      h(
        "label",
        { class: "wide check-row" },
        revive,
        h("span", {}, t("usage.revive")),
      ),
      h("div", { class: "wide form-actions" }, save),
    ),
    h("p", { class: "hint" }, t("usage.budgetHint")),
  );
}

function byAgentTable() {
  const rows = Object.entries(store.state.usage?.by_handler || {});
  if (!rows.length) return h("p", { class: "empty" }, t("usage.noCalls"));
  return h(
    "table",
    { class: "table" },
    h(
      "thead",
      {},
      h(
        "tr",
        {},
        [
          t("usage.profile"),
          t("usage.calls"),
          t("usage.tokens"),
          "≈ API",
          t("usage.unknown"),
        ].map((c) => h("th", {}, c)),
      ),
    ),
    h(
      "tbody",
      {},
      rows.map(([name, u]) =>
        h(
          "tr",
          {},
          h("td", { class: "mono" }, name),
          h("td", {}, u.calls),
          h("td", {}, formatNumber(u.tokens)),
          h("td", {}, money(u.usd)),
          h("td", {}, u.unknown || "—"),
        ),
      ),
    ),
  );
}

function dailyChart() {
  const daily = Object.entries(store.state.usage?.daily_dispatches || {});
  if (!daily.length) return h("p", { class: "hint" }, t("usage.noDays"));
  const max = Math.max(1, ...daily.map(([, n]) => n));
  const chart = svg("svg", {
    viewBox: `0 0 ${daily.length * 48} 96`,
    role: "img",
    "aria-label": t("usage.dailyTitle"),
    class: "bars",
  });
  daily.forEach(([day, n], i) => {
    const height = (n / max) * 56;
    const bar = svg("rect", {
      x: i * 48 + 10,
      y: 70 - height,
      width: 28,
      height,
      rx: 3,
      class: "usage-bar",
    });
    bar.append(svg("title", {}, `${day}: ${n}`));
    chart.append(
      bar,
      svg(
        "text",
        {
          x: i * 48 + 24,
          y: 86,
          "text-anchor": "middle",
          class: "usage-label",
        },
        day.slice(5),
      ),
    );
    chart.append(
      svg(
        "text",
        {
          x: i * 48 + 24,
          y: 66 - height,
          "text-anchor": "middle",
          class: "usage-value",
        },
        String(n),
      ),
    );
  });
  return chart;
}

function duration(minutes) {
  if (minutes == null) return "?";
  const unit =
    minutes % 1440 === 0 ? "day" : minutes % 60 === 0 ? "hour" : "minute";
  const value = minutes / (unit === "day" ? 1440 : unit === "hour" ? 60 : 1);
  return new Intl.NumberFormat(language(), {
    style: "unit",
    unit,
    unitDisplay: "long",
  }).format(value);
}

/** One window of a subscription: what is left of it and when it starts afresh. */
function windowMeter(w) {
  const left = Math.round(w.remaining_percent);
  const reset = w.resets_at ? t("usage.resets", { when: formatTime(w.resets_at, true) }) : "";
  return h(
    "div",
    { class: "meter" + (w.spent ? " is-high" : left <= 10 ? " is-high" : "") },
    h(
      "div",
      { class: "meter-head" },
      h("strong", {}, `${w.name} · ${duration(w.duration_minutes)}`),
      h("span", { class: "mono" }, t("usage.left", { percent: left })),
    ),
    h(
      "div",
      {
        class: "meter-track",
        role: "progressbar",
        "aria-label": w.name,
        "aria-valuenow": left,
        "aria-valuemax": 100,
      },
      h("span", { style: { width: left + "%" } }),
    ),
    reset ? h("small", { class: "hint" }, reset) : null,
  );
}

/** A subscription: its windows, and which agents rest until one of them resets. */
function subscriptionBlock(s) {
  const resting = s.profiles.filter((p) => p.resting_until);
  return h(
    "div",
    { class: "subscription" },
    h("h3", {}, [s.runner, s.plan].filter(Boolean).join(" · ")),
    s.error
      ? h("p", { class: "attention tone-blocked" }, t("usage.quotaUnavailable", { error: s.error }))
      : null,
    s.windows.map(windowMeter),
    resting.length
      ? h(
          "p",
          { class: "attention tone-waiting" },
          t("usage.agentsRest", {
            agents: resting.map((p) => p.name).join(", "),
            when: formatTime(Math.max(...resting.map((p) => p.resting_until)), true),
          }),
        )
      : s.windows.length
        ? h("p", { class: "hint" }, t("usage.agentsWork", { agents: s.profiles.map((p) => p.name).join(", ") }))
        : null,
  );
}

function quotaPanel() {
  const quota = store.state.usage?.subscription;
  const button = h(
    "button",
    {
      type: "button",
      id: "subscription-status",
      onclick: (e) => loadQuota(e.currentTarget),
    },
    t("usage.refreshQuota"),
  );
  const subscriptions = quota?.subscriptions || [];
  return h(
    "section",
    { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", {}, t("usage.quotaTitle")), button),
    quota?.checked_at
      ? h("small", { class: "hint" }, t("usage.quotaChecked", { when: relativeTime(quota.checked_at) }))
      : h("p", { class: "hint" }, t("usage.quotaNever")),
    subscriptions.map(subscriptionBlock),
    h("p", { class: "hint" }, t("usage.quotaHint")),
  );
}

async function loadQuota(button) {
  if (button) button.disabled = true;
  try {
    await api.post("subscription", {});
    await refresh();
  } catch (error) {
    if (button) toastError(error);
  } finally {
    if (button) button.disabled = false;
  }
}

function draw() {
  if (!root || !store.state) return;
  if (root.contains(document.activeElement) && document.activeElement.form)
    return;
  const state = store.state;
  renderUsage(
    [
      state.totals,
      state.settings,
      state.usage,
      runs().filter((r) => r.status !== "accepted").length,
      window.ffaiPreferences.language,
    ],
    () =>
      replace(
        root,
        h("p", { class: "lead" }, t("usage.lead")),
        h(
          "div",
          { class: "summary-tiles" },
          [
            [
              t("usage.tokens"),
              formatNumber(state.totals.tokens) +
                (state.totals.usage_unknown ? " + ?" : ""),
            ],
            ["≈ API", money(state.usage?.total_usd)],
            [t("usage.calls"), state.totals.calls],
            [t("usage.accepted"), state.totals.accepted],
          ].map(([label, value]) =>
            h(
              "div",
              { class: "tile" },
              h("small", {}, label),
              h("strong", {}, value),
            ),
          ),
        ),
        h(
          "div",
          { class: "two-columns" },
          budgetPanel(),
          quotaPanel(),
          h(
            "section",
            { class: "panel" },
            h("h2", {}, t("usage.byAgent")),
            byAgentTable(),
          ),
          h(
            "section",
            { class: "panel" },
            h("h2", {}, t("usage.dailyTitle")),
            dailyChart(),
          ),
        ),
        h(
          "section",
          { class: "explain" },
          h("h3", {}, t("usage.howTitle")),
          h(
            "ul",
            {},
            ["1", "2", "3", "4"].map((k) => h("li", {}, t("usage.how" + k))),
          ),
        ),
      ),
  );
}

registerView({
  id: "usage",
  order: 5,
  title: "nav.usage",
  glyph: "◔",
  mount(container) {
    root = container;
    renderUsage(null, () => {});
    draw();
  },
  update() {
    draw();
  },
  unmount() {
    root = null;
  },
});
