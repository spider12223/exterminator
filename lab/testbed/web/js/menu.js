// Small dropdown helper, used by the account menu and the sort menu.
export function setupDropdown(button, menu, { onSelect } = {}) {
  function setOpen(open) {
    menu.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
  }

  button.addEventListener("click", (event) => {
    event.stopPropagation();
    setOpen(menu.hidden);
  });

  menu.addEventListener("click", (event) => {
    const item = event.target.closest('[role="menuitem"], [role="option"]');
    if (!item) return;
    if (onSelect) onSelect(item);
    setOpen(false);
  });

  document.addEventListener("click", (event) => {
    if (!menu.contains(event.target)) setOpen(false);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setOpen(false);
  });
}
