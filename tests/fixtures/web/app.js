// bugcam demo shop. index.html runs the buggy paths; fixed.html (<body data-fixed>) the fixed ones.
const fixed = document.body.hasAttribute("data-fixed");
const countEl = document.getElementById("cart-count");
const emptyBanner = document.getElementById("cart-empty");
let count = 0;

document.getElementById("buy").addEventListener("click", () => {
  if (fixed) {
    count += 1;
    countEl.textContent = String(count);
    return;
  }
  // BUG (flash of the wrong state): the cart is re-rendered from an empty state first, so the red
  // "Your cart is empty" banner is painted for one frame before the real count lands.
  emptyBanner.classList.add("show");
  countEl.textContent = "0";
  requestAnimationFrame(() => requestAnimationFrame(() => {
    count += 1;
    emptyBanner.classList.remove("show");
    countEl.textContent = String(count);
  }));
});

// BUG (failing request): the buggy page asks for api/stock.json, which does not exist (HTTP 404).
fetch(fixed ? "stock.json" : "api/stock.json")
  .then((r) => {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  })
  .then((s) => { document.getElementById("stock").textContent = s.inStock + " in stock"; })
  .catch((e) => console.error("stock check failed: " + e.message));
