// Cart page: renders the items and fills in the order summary.
const CART = [
  { id: "trail-runner", name: "Trail Runner", size: 42, price: 89, qty: 1, image: "img/trail-runner.svg" },
  { id: "canvas-high", name: "Canvas High", size: 41, price: 59, qty: 2, image: "img/canvas-high.svg" },
];
const FREE_SHIPPING_FROM = 50;
const SHIPPING_FEE = 4.9;

const money = (n) => `$${n.toFixed(2)}`;

function itemHtml(item) {
  return `
    <li class="cart-item">
      <img src="${item.image}" alt="${item.name}" class="cart-thumb">
      <div class="cart-info">
        <h2>${item.name}</h2>
        <p>Size ${item.size}, qty ${item.qty}</p>
      </div>
      <span class="cart-price">${money(item.price * item.qty)}</span>
    </li>`;
}

export function renderCart(items) {
  document.getElementById("cart-items").innerHTML = items.map(itemHtml).join("");
  const subtotal = items.reduce((sum, item) => sum + item.price * item.qty, 0);
  const shipping = subtotal >= FREE_SHIPPING_FROM ? 0 : SHIPPING_FEE;
  document.getElementById("shipping").textContent = shipping ? money(shipping) : "Free";
  document.getElementById("subtotal").textContent = money(subtotal);
  document.getElementById("total").textContent = money(subtotal + shipping);
}

renderCart(CART);
