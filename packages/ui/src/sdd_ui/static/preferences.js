/* Apply saved preferences before styles, including when an extension triggers reload. */
(() => {
  const read = (key, fallback) => {
    try {
      return localStorage.getItem(key) || fallback;
    } catch {
      return fallback;
    }
  };
  const media = matchMedia("(prefers-color-scheme: dark)");
  let theme = read("ffai-theme", "light");
  if (!["light", "dark", "system"].includes(theme)) theme = "light";
  let language = read("ffai-language", "ru");
  if (!["ru", "en"].includes(language)) language = "ru";
  const apply = () => {
    document.documentElement.dataset.theme =
      theme === "system" ? (media.matches ? "dark" : "light") : theme;
    document.documentElement.lang = language;
  };
  window.ffaiPreferences = {
    get language() {
      return language;
    },
    get theme() {
      return theme;
    },
    set(key, value) {
      if (key === "theme" && ["light", "dark", "system"].includes(value))
        theme = value;
      if (key === "language" && ["ru", "en"].includes(value)) language = value;
      try {
        localStorage.setItem("ffai-" + key, value);
      } catch {}
      apply();
      document.dispatchEvent(new Event("preferences-changed"));
    },
  };
  apply();
  media.addEventListener("change", () => {
    if (theme === "system") apply();
  });
})();
