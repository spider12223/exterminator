import { test } from "node:test";
import assert from "node:assert/strict";
import { applyDiscount } from "../src/discount.js";

test("SAVE10 applies the discount", () => {
  assert.equal(applyDiscount(100, "SAVE10"), 85);
});

test("unknown codes change nothing", () => {
  assert.equal(applyDiscount(100, "NOPE"), 100);
});
