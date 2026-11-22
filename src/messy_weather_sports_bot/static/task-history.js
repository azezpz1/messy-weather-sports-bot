let openExecutions = [];
let focusedControl = "";

document.addEventListener("htmx:beforeSwap", ({ detail }) => {
  const panel = detail.target;
  if (panel.id !== "run-status" || !detail.shouldSwap) return;

  openExecutions = Array.from(panel.querySelectorAll("details[open]"), item => item.id);
  const active = document.activeElement;
  focusedControl = active && panel.contains(active) ? active.id : "";
});

document.addEventListener("htmx:afterSwap", ({ detail }) => {
  if (detail.elt.id !== "run-status") return;

  for (const id of openExecutions) {
    document.getElementById(id)?.setAttribute("open", "");
  }
  if (focusedControl) {
    document.getElementById(focusedControl)?.focus({ preventScroll: true });
  }
});
