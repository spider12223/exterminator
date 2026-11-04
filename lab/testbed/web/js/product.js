// Product page: info tabs and the add-to-cart button.
import { setupTabs } from "./tabs.js";

setupTabs(document.getElementById("product-tabs"));

const buy = document.getElementById("buy");
buy.addEventListener("click", () => {
  buy.textContent = "Added to cart";
});
