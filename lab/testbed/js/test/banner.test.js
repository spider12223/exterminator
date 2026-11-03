import { test } from "node:test";
import assert from "node:assert/strict";
import { welcomeBanner } from "../src/banner.js";

test("banner greets the user by name", () => {
  assert.match(welcomeBanner("Sam"), /Sam!$/);
});
