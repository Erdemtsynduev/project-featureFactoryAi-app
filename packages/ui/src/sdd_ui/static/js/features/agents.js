/* Agents: installed CLIs, the named profiles steps run with, and rotation.
 *
 * A profile is "which CLI, which model, which rights"; workflow steps name a
 * profile (claude, codex). Rotation says what happens when a profile hits a
 * limit: it rests until the reset and its fallback takes the next attempts.
 * Discovery and sign-in checks never call a model. */

import * as api from "../core/api.js";
import { h, memo, replace } from "../core/dom.js";
import { formatNumber, formatTime, money, t } from "../core/i18n.js";
import { refresh, store } from "../core/store.js";
import { attempt, toast, toastError } from "../ui/toast.js";
import { registerView } from "./shell.js";

const FAILURES = ["usage_limit", "rate_limit", "authentication", "unreachable"];
const models = new Map();
let root = null;
let nodes = {};
const renderDiscovery = memo();
const renderProfiles = memo();

function pill(text, tone = "") {
  return h("span", { class: "pill " + tone }, text);
}

function usageOf(names) {
  const total = { calls: 0, tokens: 0, unknown: 0, usd: 0 };
  for (const name of names) {
    const u = store.state.usage?.by_handler?.[name] || {};
    total.calls += u.calls || 0;
    total.tokens += u.tokens || 0;
    total.unknown += u.unknown || 0;
    total.usd += u.usd || 0;
  }
  return total;
}

function stats(names) {
  const u = usageOf(names);
  return h(
    "div",
    { class: "stats" },
    [
      [t("usage.calls"), u.calls],
      [t("usage.tokens"), formatNumber(u.tokens)],
      ["≈ API", money(u.usd)],
    ].map(([label, value]) =>
      h("span", {}, h("b", {}, String(value)), " ", label),
    ),
    u.unknown ? h("small", { class: "hint" }, t("usage.someUnknown")) : null,
  );
}

/* Discovery ------------------------------------------------------------------ */

async function loadModels(adapter, discovery, list, model) {
  if (!models.has(adapter)) {
    try {
      models.set(adapter, (await api.post("models", { adapter })).models);
    } catch {
      models.set(adapter, discovery.suggested_models || []);
    }
  }
  replace(
    list,
    models.get(adapter).map((m) => h("option", { value: m })),
  );
  if (!model.value && models.get(adapter).length)
    model.value = models.get(adapter)[0];
}

function connectForm(discovery, configured) {
  const adapter = discovery.adapter;
  const name = h("input", {
    required: true,
    value: configured[0]?.[0] || adapter,
    "aria-label": t("agents.profileName"),
  });
  const list = h("datalist", { id: "models-" + adapter });
  const model = h("input", {
    required: true,
    list: list.id,
    value: configured[0]?.[1].model || "",
    placeholder: t("agents.modelPlaceholder"),
    "aria-label": t("agents.model"),
  });
  const rights = h(
    "select",
    { "aria-label": t("agents.rights") },
    h("option", { value: "workspace-write" }, t("agents.canWrite")),
    h("option", { value: "read-only" }, t("agents.readOnly")),
  );
  rights.value = configured[0]?.[1].permissions || "workspace-write";
  loadModels(adapter, discovery, list, model);
  const submit = h(
    "button",
    { type: "submit", class: "primary" },
    configured.length ? t("agents.update") : t("agents.connect"),
  );
  return h(
    "form",
    {
      class: "connect-form",
      onsubmit: async (event) => {
        event.preventDefault();
        submit.disabled = true;
        await attempt(
          async () => {
            await api.post("connect", {
              adapter,
              argv: discovery.selected,
              name: name.value.trim(),
              model: model.value.trim(),
              permissions: rights.value,
            });
            await refresh();
          },
          t("agents.connected", { name: name.value.trim() }),
        );
        submit.disabled = false;
      },
    },
    h(
      "label",
      { class: "field" },
      h("span", {}, t("agents.profileName")),
      name,
    ),
    h(
      "label",
      { class: "field" },
      h("span", {}, t("agents.model")),
      model,
      list,
    ),
    h(
      "label",
      { class: "field wide" },
      h("span", {}, t("agents.rights")),
      rights,
    ),
    submit,
  );
}

function discoveryCard(discovery) {
  const config = store.state.profile_config;
  const configured = Object.entries(config.profiles || {}).filter(
    ([, p]) => config.runners?.[p.runner]?.adapter === discovery.adapter,
  );
  const installed = discovery.status === "installed";
  const login = discovery.authentication;
  const chosen = discovery.candidates.find(
    (c) => discovery.selected && c.argv.join() === discovery.selected.join(),
  );
  return h(
    "article",
    { class: "card agent-card" },
    h(
      "div",
      { class: "agent-head" },
      h("strong", { class: "mono" }, discovery.adapter),
      installed
        ? pill(t("agents.installed"), "tone-done")
        : discovery.status === "missing"
          ? pill(t("agents.missing"))
          : discovery.status === "ambiguous"
            ? pill(t("agents.ambiguous"), "tone-waiting")
            : pill(t("agents.broken"), "tone-blocked"),
      installed
        ? login === "authenticated"
          ? pill(t("agents.signedIn"), "tone-done")
          : login === "not_authenticated"
            ? pill(t("agents.signInNeeded"), "tone-blocked")
            : pill(t("agents.signInUnknown"))
        : null,
    ),
    chosen?.version ? h("small", { class: "mono" }, chosen.version) : null,
    discovery.selected
      ? h("small", { class: "mono path" }, discovery.selected[0])
      : null,
    login === "not_authenticated"
      ? h(
          "p",
          { class: "hint" },
          t("agents.signInHint", { cli: discovery.adapter }),
        )
      : null,
    configured.length ? stats(configured.map(([n]) => n)) : null,
    !discovery.selected
      ? null
      : configured.length
        ? h(
            "details",
            { class: "connect-details" },
            h("summary", {}, t("agents.editConnection")),
            connectForm(discovery, configured),
          )
        : connectForm(discovery, configured),
  );
}

/* A form being edited is never replaced under the operator's hands. */
const editing = (node) => node.contains(document.activeElement);

function drawDiscovery() {
  if (editing(nodes.discovery)) return;
  const found = store.state.agent_discovery || [];
  renderDiscovery(
    [
      found,
      store.state.profile_config,
      store.state.usage?.by_handler,
      window.ffaiPreferences.language,
    ],
    () =>
      replace(
        nodes.discovery,
        found.length
          ? found.map(discoveryCard)
          : h("div", { class: "empty" }, t("agents.discoverFirst")),
      ),
  );
}

/* Profiles and rotation ------------------------------------------------------ */

function rotationForm(name, config) {
  const rule = config.rotation?.[name];
  const others = Object.keys(config.profiles || {}).filter((n) => n !== name);
  const target = h(
    "select",
    { "aria-label": t("rotation.fallbackFor", { name }) },
    h("option", { value: "" }, t("rotation.none")),
    others.map((n) => h("option", { value: n }, n)),
  );
  target.value = rule?.fallbacks?.[0] || "";
  const cooldown = h("input", {
    type: "number",
    min: "1",
    max: "10080",
    value: rule?.cooldown_minutes ?? 60,
    "aria-label": t("rotation.rest"),
  });
  const retry = h("input", {
    type: "number",
    min: "5",
    max: "3600",
    value: rule?.retry_seconds ?? 20,
    "aria-label": t("rotation.retry"),
  });
  const on = new Set(
    rule?.on || ["usage_limit", "rate_limit", "authentication"],
  );
  const triggers = FAILURES.map((key) =>
    h(
      "label",
      {},
      h("input", { type: "checkbox", value: key, checked: on.has(key) }),
      h("span", {}, t("failure." + key)),
    ),
  );
  const body = h(
    "div",
    { class: "rotation-body" },
    h("label", { class: "field" }, h("span", {}, t("rotation.rest")), cooldown),
    h("label", { class: "field" }, h("span", {}, t("rotation.retry")), retry),
    h(
      "fieldset",
      { class: "wide triggers" },
      h("legend", {}, t("rotation.when")),
      triggers,
    ),
  );
  const sync = () => (body.hidden = !target.value);
  target.addEventListener("change", sync);
  sync();
  const save = h("button", { type: "submit" }, t("rotation.save"));
  return h(
    "form",
    {
      class: "rotation-form",
      onsubmit: async (event) => {
        event.preventDefault();
        save.disabled = true;
        await attempt(
          async () => {
            await api.post("rotation", {
              name,
              fallbacks: target.value ? [target.value] : [],
              on: triggers
                .map((l) => l.querySelector("input"))
                .filter((i) => i.checked)
                .map((i) => i.value),
              cooldown_minutes: Number(cooldown.value),
              retry_seconds: Number(retry.value),
            });
            await refresh();
          },
          target.value
            ? t("rotation.saved", { name, target: target.value })
            : t("rotation.cleared", { name }),
        );
        save.disabled = false;
      },
    },
    h(
      "label",
      { class: "field wide" },
      h("span", {}, t("rotation.fallback")),
      target,
    ),
    body,
    h("div", { class: "form-actions" }, save),
  );
}

function profileCard(name, profile, config) {
  const runner = config.runners?.[profile.runner] || {};
  const resting = store.state.cooldowns?.[name];
  return h(
    "article",
    { class: "card profile-card" },
    h(
      "div",
      { class: "agent-head" },
      h("strong", { class: "mono" }, name),
      pill(`${runner.adapter || profile.runner} · ${profile.model}`),
      pill(
        profile.permissions === "workspace-write"
          ? t("agents.canWrite")
          : t("agents.readOnly"),
      ),
    ),
    resting
      ? h(
          "div",
          { class: "attention tone-waiting" },
          h(
            "span",
            {},
            t("rotation.resting", {
              until: formatTime(resting.until, true),
              reason: t("failure." + resting.reason),
            }),
          ),
          h(
            "button",
            {
              type: "button",
              onclick: () =>
                attempt(
                  async () => {
                    await api.post("wake", { name });
                    await refresh();
                  },
                  t("rotation.woken", { name }),
                ),
            },
            t("rotation.wake"),
          ),
        )
      : h("small", { class: "hint" }, t("rotation.available")),
    stats([name]),
    rotationForm(name, config),
  );
}

function drawProfiles() {
  if (editing(nodes.profiles)) return;
  const config = store.state.profile_config;
  const entries = Object.entries(config.profiles || {});
  renderProfiles(
    [
      config,
      store.state.cooldowns,
      store.state.usage?.by_handler,
      window.ffaiPreferences.language,
    ],
    () => {
      replace(
        nodes.profiles,
        entries.length
          ? entries.map(([name, profile]) => profileCard(name, profile, config))
          : h("div", { class: "empty" }, t("agents.noProfiles")),
      );
      if (document.activeElement !== nodes.json)
        nodes.json.value = JSON.stringify(config, null, 2);
    },
  );
}

/* View ----------------------------------------------------------------------- */

async function discover(button) {
  button.disabled = true;
  toast(t("agents.discovering"));
  try {
    const found = await api.post("discover", {});
    models.clear();
    await refresh();
    toast(
      t("agents.discovered", {
        installed: found.filter((d) => d.status === "installed").length,
        ready: found.filter((d) => d.authentication === "authenticated").length,
      }),
      { tone: "success" },
    );
  } catch (error) {
    toastError(error);
  } finally {
    button.disabled = false;
  }
}

function build() {
  const discoverButton = h(
    "button",
    {
      type: "button",
      id: "discover-agents",
      class: "primary",
      onclick: (e) => discover(e.currentTarget),
    },
    t("agents.discover"),
  );
  nodes.discovery = h("div", { id: "agent-status", class: "card-grid" });
  nodes.profiles = h("div", { id: "profiles-list", class: "card-grid" });
  nodes.json = h("textarea", {
    id: "profiles-json",
    rows: "14",
    spellcheck: "false",
  });
  return h(
    "div",
    { class: "agents" },
    h(
      "div",
      { class: "view-intro" },
      h("p", { class: "lead" }, t("agents.lead")),
      h("div", { class: "toolbar" }, discoverButton),
    ),
    h(
      "section",
      { class: "explain" },
      h("h3", {}, t("agents.howTitle")),
      h(
        "ol",
        {},
        ["1", "2", "3", "4"].map((k) => h("li", {}, t("agents.how" + k))),
      ),
    ),
    h("h2", {}, t("agents.cliTitle")),
    nodes.discovery,
    h("h2", {}, t("agents.profilesTitle")),
    nodes.profiles,
    h(
      "details",
      { class: "panel" },
      h("summary", {}, t("agents.manual")),
      h("p", { class: "hint" }, t("agents.manualHint")),
      nodes.json,
      h(
        "div",
        { class: "form-actions" },
        h(
          "button",
          {
            type: "button",
            id: "save-profiles",
            class: "primary",
            onclick: () =>
              attempt(async () => {
                await api.post("profiles", {
                  config: JSON.parse(nodes.json.value),
                });
                await refresh();
              }, t("agents.saved")),
          },
          t("agents.saveJson"),
        ),
      ),
    ),
  );
}

function draw() {
  if (!root || !store.state) return;
  drawDiscovery();
  drawProfiles();
}

registerView({
  id: "agents",
  order: 4,
  title: "nav.agents",
  glyph: "◎",
  mount(container) {
    root = container;
    replace(root, build());
    renderDiscovery(null, () => {});
    renderProfiles(null, () => {});
    draw();
  },
  update(_, reason) {
    if (reason === "language" && root) this.mount(root);
    else draw();
  },
  unmount() {
    root = null;
  },
});
