// Tiny in-memory "database" with realistic async latency.
const tables = { orders: new Map(), carts: new Map(), audit: [] };
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export async function saveOrder(order) {
  await delay(20);
  tables.orders.set(order.id, { ...order });
  return order.id;
}

export async function getOrder(id) {
  await delay(1);
  return tables.orders.get(id) ?? null;
}

export async function saveCart(cart) {
  await delay(20);
  tables.carts.set(cart.userId, { ...cart, items: [...cart.items] });
}

export async function getCart(userId) {
  await delay(1);
  return tables.carts.get(userId) ?? null;
}

export async function appendAudit(entry) {
  await delay(3);
  tables.audit.push(entry);
}

// Test helper: read an order without the simulated latency.
export function _peekOrder(id) {
  return tables.orders.get(id) ?? null;
}

export function _reset() {
  tables.orders.clear();
  tables.carts.clear();
  tables.audit.length = 0;
}
