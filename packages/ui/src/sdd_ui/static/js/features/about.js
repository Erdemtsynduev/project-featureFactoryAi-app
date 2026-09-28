/* Engine version in the rail and the "About" dialog with every library version.
 * All libraries are released together; a mixed installation is flagged. */

import { byId, h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { store, subscribe } from "../core/store.js";
import { openDialog } from "../ui/dialog.js";

function renderButton() {
  const versions = store.state?.versions;
  const button = byId("about");
  if (!versions) return;
  button.textContent = t("about.engine", { version: versions.engine });
  button.classList.toggle("is-mixed", !versions.consistent);
  button.title = versions.consistent ? t("about.open") : t("about.mixed");
}

export function openAbout() {
  const versions = store.state?.versions || { packages: {} };
  openDialog({
    title: t("about.title"),
    subtitle: t("about.subtitle", { version: versions.engine }),
    size: "narrow",
    body: [
      versions.consistent === false
        ? h("p", { class: "attention tone-blocked" }, t("about.mixed"))
        : null,
      h(
        "table",
        { class: "table" },
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            h("th", {}, t("about.library")),
            h("th", {}, t("about.version")),
          ),
        ),
        h(
          "tbody",
          {},
          Object.entries(versions.packages).map(([name, version]) =>
            h(
              "tr",
              {},
              h("td", { class: "mono" }, name),
              h("td", { class: "mono" }, version || t("about.notInstalled")),
            ),
          ),
        ),
      ),
      h("p", { class: "hint" }, t("about.hint")),
    ],
  });
}

export function startAbout() {
  byId("about").onclick = openAbout;
  subscribe(renderButton);
  document.addEventListener("preferences-changed", renderButton);
  renderButton();
}
