// Light / dark theme. The theme is a class on <html>: theme-light or theme-dark.
const root = document.documentElement;

export function currentTheme() {
  return root.classList.contains("theme-dark") ? "dark" : "light";
}

export function setTheme(next) {
  const prev = currentTheme();
  if (next === prev) return;
  root.classList.remove(`theme-${prev}`);
  // apply the new theme on the next frame so the old colours are fully cleared first
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      root.classList.add(`theme-${next}`);
    });
  });
}

export function toggleTheme() {
  setTheme(currentTheme() === "dark" ? "light" : "dark");
}
