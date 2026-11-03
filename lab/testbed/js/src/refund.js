import { getOrder, saveOrder, appendAudit } from "./db.js";

export async function refundOrder(id) {
  const order = await getOrder(id);
  if (!order) {
    throw new Error("order not found");
  }
  const refunded = { ...order, status: "refunded" };
  saveOrder(refunded);
  await appendAudit({ type: "refund", id });
  return refunded;
}
