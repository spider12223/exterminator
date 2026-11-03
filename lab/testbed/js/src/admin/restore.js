import { persist } from "../orderService.js";

export async function restoreOrder(order) {
  await persist({ ...order, status: "paid" });
  return order.id;
}
