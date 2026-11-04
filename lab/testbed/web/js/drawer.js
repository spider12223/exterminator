// Filters drawer on the home page.
export function setupDrawer(button, drawer) {
  const close = drawer.querySelector("[data-close]");

  function setOpen(open) {
    drawer.classList.remove(open ? "is-closed" : "is-open");
    drawer.classList.add(open ? "is-open" : "is-closed");
    button.setAttribute("aria-expanded", String(open));
    if (open) {
      // focus the first filter once the drawer is on screen
      requestAnimationFrame(() => drawer.querySelector("input").focus({ preventScroll: true }));
    }
  }

  button.addEventListener("click", () => setOpen(!drawer.classList.contains("is-open")));
  close.addEventListener("click", () => setOpen(false));
}
