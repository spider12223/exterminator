// Small notice in the bottom-right corner. It floats above the page instead of taking up space in it.
export function showToast(text, { delay = 0, duration = 5000 } = {}) {
  setTimeout(() => {
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.setAttribute("role", "status");
    toast.textContent = text;
    Object.assign(toast.style, { position: "fixed", right: "24px", bottom: "24px", zIndex: "50" });
    document.body.append(toast);
    setTimeout(() => toast.remove(), duration);
  }, delay);
}
