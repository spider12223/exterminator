import { getOrder, saveOrder } from "../db.js";

// Nightly job: apply a price factor to a batch of orders.
export async function repriceAll(ids, factor) {
  ids.forEach(async (id) => {
    const order = await getOrder(id);
    if (order) {
      await saveOrder({ ...order, total: Math.round(order.total * factor) });
    }
  });
  return ids.length;
}
