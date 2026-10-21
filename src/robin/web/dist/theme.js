// Runs before the app so the first paint is already in the chosen appearance; mirrors resolveTheme in src/theme.ts.
(function () {
  var choice = null;
  try {
    choice = localStorage.getItem("robin.theme");
  } catch (e) {
    /* not remembered */
  }
  var light =
    choice === "light" ||
    (choice !== "dark" && !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches));
  document.documentElement.dataset.theme = light ? "light" : "dark";
})();
