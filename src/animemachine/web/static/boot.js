(() => {
  const supported = new Set(["zh-Hans", "en", "ja"]);
  const normalize = (value) => {
      const code = String(value || "").toLowerCase();
      return code.startsWith("zh")
        ? "zh-Hans"
        : code.startsWith("ja")
          ? "ja"
          : code.startsWith("en")
            ? "en"
            : "";
    },
    detected = (navigator.languages || [navigator.language])
      .map(normalize)
      .find((value) => supported.has(value));
  let language = detected || "en";
  try {
    const saved = localStorage.getItem("anm-language");
    if (supported.has(saved)) language = saved;
  } catch (_) {
    // Storage may be unavailable; the browser/system language remains usable.
  }
  document.documentElement.lang = language;
  let theme = "system";
  try {
    const savedTheme = localStorage.getItem("anm-theme");
    if (["system", "light", "dark"].includes(savedTheme)) theme = savedTheme;
  } catch (_) {}
  const resolvedTheme = theme === "system"
    ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : theme;
  document.documentElement.dataset.theme = resolvedTheme;
  document.addEventListener("DOMContentLoaded", () => {
    const selector = document.getElementById("language");
    if (selector) selector.value = language;
  });
})();
