// Tabs on the product page (Details / Reviews / Shipping).
const nextFrame = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));

export function setupTabs(root) {
  const tabs = [...root.querySelectorAll('[role="tab"]')];
  const panels = tabs.map((tab) => document.getElementById(tab.getAttribute("aria-controls")));

  async function select(index) {
    const current = panels.find((panel) => !panel.hidden);
    if (current === panels[index]) return;
    tabs.forEach((tab, i) => tab.setAttribute("aria-selected", String(i === index)));
    if (current) current.hidden = true;
    await nextFrame();
    panels[index].hidden = false;
  }

  tabs.forEach((tab, i) => tab.addEventListener("click", () => select(i)));
}
