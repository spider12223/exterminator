import { saveOrder, getOrder, saveCart } from "./db.js";

function orderTotal(items) {
  return items.reduce((sum, item) => sum + item.price * item.qty, 0);
}

export async function placeOrder(cart) {
  const order = {
    id: `o-${cart.userId}-${cart.items.length}`,
    userId: cart.userId,
    items: cart.items,
    total: orderTotal(cart.items),
    status: "paid",
  };
  saveOrder(order);
  const saved = await getOrder(order.id);
  if (!saved) {
    throw new Error("order not found");
  }
  // empty the cart now that it has been ordered
  saveCart({ userId: cart.userId, items: [] });
  return saved;
}
