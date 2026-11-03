import { test } from "node:test";
import assert from "node:assert/strict";
import { paginate } from "../src/pagination.js";

const items = Array.from({ length: 25 }, (_, i) => `item-${i + 1}`);

test("page 1 starts at the first item", () => {
  const { items: page } = paginate(items, 1, 10);
  assert.equal(page[0], "item-1");
  assert.equal(page.length, 10);
});

test("last page holds the remainder", () => {
  const { items: page } = paginate(items, 3, 10);
  assert.deepEqual(page, ["item-21", "item-22", "item-23", "item-24", "item-25"]);
});
