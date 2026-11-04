// Loaded on every page: theme switch and account menu in the header.
import { toggleTheme } from "./theme.js";
import { setupDropdown } from "./menu.js";

document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
setupDropdown(document.getElementById("account-button"), document.getElementById("account-menu"));
