/* In-app history: tabs and task cards are history entries, so the browser's Back
 * button, mouse back and the "←" button return to the previous place. The hash
 * holds only a bare token (#tasks, #flows, #settings, #task-<id>). */
"use strict";
(() => {
  const TABS = ["tasks", "flows", "settings"];
  let applying = false;
  let depth = 0;
  const back = $("nav-back");
  const sync = () => (back.disabled = depth === 0);

  function place() {
    const hash = location.hash.slice(1);
    if (hash.startsWith("task-")) return { tab: "tasks", task: hash.slice(5) };
    return { tab: TABS.includes(hash) ? hash : "tasks", task: null };
  }
  function push(hash) {
    if (applying || location.hash === "#" + hash) return;
    history.pushState({ depth: depth + 1 }, "", "#" + hash);
    depth++;
    sync();
  }

  const showTabBase = showTab;
  showTab = (name) => {
    showTabBase(name);
    push(name);
  };
  const openDetailBase = openDetail;
  openDetail = async (id, ...rest) => {
    const result = await openDetailBase(id, ...rest);
    if (detailId === id) push("task-" + id);
    return result;
  };

  $("task-dialog").addEventListener("close", () => {
    // Closing a card from the page steps back so history stays linear.
    if (!applying && location.hash.startsWith("#task-") && depth > 0)
      history.back();
  });
  window.addEventListener("popstate", (event) => {
    depth = event.state?.depth || 0;
    sync();
    const target = place();
    applying = true;
    try {
      showTabBase(target.tab);
      if (target.task) act(() => openDetailBase(target.task));
      else if ($("task-dialog").open) $("task-dialog").close();
    } finally {
      applying = false;
    }
  });
  back.onclick = () => history.back();

  // Start where the address points, without adding an entry.
  history.replaceState({ depth: 0 }, "", location.hash || "#tasks");
  const start = place();
  applying = true;
  showTabBase(start.tab);
  applying = false;
  if (start.task)
    document.addEventListener(
      "ffai-ready",
      () => act(() => openDetailBase(start.task)),
      { once: true },
    );
  sync();
})();
