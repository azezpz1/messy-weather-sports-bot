(() => {
  const root = document.documentElement;
  const storageKey = "messy-weather.appearance";

  try {
    const saved = localStorage.getItem(storageKey);
    if (saved === "light" || saved === "dark") root.dataset.theme = saved;
  } catch {
    // Use the system theme if storage is unavailable.
  }

  document.addEventListener("DOMContentLoaded", () => {
    const control = document.getElementById("appearance");
    control.value = root.dataset.theme || "system";
    control.addEventListener("change", () => {
      const theme = control.value;
      if (theme === "system") delete root.dataset.theme;
      else root.dataset.theme = theme;

      try {
        if (theme === "system") localStorage.removeItem(storageKey);
        else localStorage.setItem(storageKey, theme);
      } catch {
        // The choice still applies to the current page.
      }
    });
  });
})();
