// Home page: product grid toolbar (layout switch, filters, sort) and the promo strip.
import { setupDropdown } from "./menu.js";
import { setupViewSwitch } from "./view.js";
import { setupDrawer } from "./drawer.js";
import { showPromo } from "./promo.js";

const grid = document.getElementById("product-grid");

setupViewSwitch(grid, [...document.querySelectorAll(".view-switch button")]);
setupDrawer(document.getElementById("filters-button"), document.getElementById("filters"));

const SORTS = {
  featured: (a, b) => Number(a.dataset.rank) - Number(b.dataset.rank),
  "price-asc": (a, b) => Number(a.dataset.price) - Number(b.dataset.price),
  "price-desc": (a, b) => Number(b.dataset.price) - Number(a.dataset.price),
  name: (a, b) => a.dataset.name.localeCompare(b.dataset.name),
};

const sortButton = document.getElementById("sort-button");
setupDropdown(sortButton, document.getElementById("sort-menu"), {
  onSelect(option) {
    grid.append(...[...grid.children].sort(SORTS[option.dataset.sort]));
    for (const other of option.parentElement.children) {
      other.setAttribute("aria-selected", String(other === option));
    }
    sortButton.textContent = `Sort: ${option.textContent}`;
  },
});

showPromo();
