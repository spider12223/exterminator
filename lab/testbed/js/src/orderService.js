import { saveOrder } from "./db.js";

// Thin wrapper used by the admin tools; callers await the returned promise.
export function persist(order) {
  return saveOrder(order);
}
