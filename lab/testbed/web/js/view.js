// Grid / list switch above the product grid on the home page.
export function setupViewSwitch(grid, buttons) {
  for (const button of buttons) {
    button.addEventListener("click", () => {
      const view = button.dataset.view;
      if (grid.dataset.view === view) return;
      for (const other of buttons) other.setAttribute("aria-pressed", String(other === button));
      // drop the old layout first, then switch once the browser has caught up
      grid.removeAttribute("data-view");
      setTimeout(() => {
        grid.dataset.view = view;
      }, 50);
    });
  }
}
