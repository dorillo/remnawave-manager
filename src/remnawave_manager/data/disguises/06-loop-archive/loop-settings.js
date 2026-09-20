(() => {
  let theme, lang, paused;
  try {
    theme = localStorage.getItem("loop:theme");
    lang = localStorage.getItem("loop:lang");
    paused = localStorage.getItem("loop:paused");
  } catch {}
  document.documentElement.dataset.theme = ["light", "dark"].includes(theme)
    ? theme
    : matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  document.documentElement.lang = lang === "en" ? "en" : "ru";
  document.documentElement.dataset.paused = String(
    ["true", "false"].includes(paused)
      ? paused === "true"
      : matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
})();
