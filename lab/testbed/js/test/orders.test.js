import { test } from "node:test";
import assert from "node:assert/strict";
import { _reset, saveOrder } from "../src/db.js";
import { refundOrder } from "../src/refund.js";

test("refundOrder marks the order refunded", async () => {
  _reset();
  await saveOrder({ id: "o-1", userId: "u1", items: [], total: 10, status: "paid" });
  const result = await refundOrder("o-1");
  assert.equal(result.status, "refunded");
});
