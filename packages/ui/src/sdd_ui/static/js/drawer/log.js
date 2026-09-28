/* Log tab: the task's flight log, engine journal, incident export and recovery. */

import * as api from "../core/api.js";
import { h, replace } from "../core/dom.js";
import { formatTime, t } from "../core/i18n.js";
import { toastError } from "../ui/toast.js";
import { availableCommands, commandButton } from "../features/commands.js";
import { section } from "./parts.js";

export function logTab(detail) {
  const run = detail.run;
  const box = h(
    "div",
    { class: "log-box" },
    h("p", { class: "hint" }, t("log.loading")),
  );
  api
    .get("log", { run: run.id, limit: 200 })
    .then((entries) =>
      replace(
        box,
        entries.length
          ? h(
              "ol",
              { class: "timeline" },
              entries
                .slice()
                .reverse()
                .map((e) =>
                  h(
                    "li",
                    { class: "level-" + e.level },
                    h("time", {}, formatTime(e.at, true)),
                    h(
                      "strong",
                      {},
                      t("log.kind." + e.kind) === "log.kind." + e.kind
                        ? e.kind
                        : t("log.kind." + e.kind),
                    ),
                    h(
                      "span",
                      {},
                      [e.action, e.command, e.reason, e.error, e.outcome]
                        .filter(Boolean)
                        .join(" · "),
                    ),
                  ),
                ),
            )
          : h("p", { class: "hint" }, t("log.empty")),
      ),
    )
    .catch(toastError);
  const recoverable = availableCommands(run).some(
    ([name]) => name === "recover",
  );
  return [
    section(
      t("log.incident"),
      h("p", { class: "hint" }, t("log.incidentHint")),
      h(
        "div",
        { class: "form-actions" },
        h(
          "button",
          { type: "button", onclick: () => downloadIncident(run.id) },
          t("log.download"),
        ),
        recoverable
          ? commandButton(["recover", "command.recover", "primary"], run)
          : null,
      ),
    ),
    section(t("log.flight"), box),
    section(
      t("log.journal"),
      h(
        "ol",
        { class: "timeline" },
        detail.events
          .slice()
          .reverse()
          .map((e) =>
            h(
              "li",
              {},
              h("time", {}, formatTime(e.at, true)),
              h("strong", {}, e.kind),
              h(
                "span",
                { class: "mono" },
                String(e.detail || "").slice(0, 400),
              ),
            ),
          ),
      ),
    ),
  ];
}

async function downloadIncident(id) {
  try {
    const record = await api.get("incident", { id });
    const blob = new Blob([JSON.stringify(record, null, 2)], {
      type: "application/json",
    });
    const link = h("a", {
      href: URL.createObjectURL(blob),
      download: `incident-${id}.json`,
    });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  } catch (error) {
    toastError(error);
  }
}
