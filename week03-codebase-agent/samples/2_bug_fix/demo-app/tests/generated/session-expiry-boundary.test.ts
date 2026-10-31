import assert from "node:assert/strict";
import test from "node:test";

import { isSessionExpired } from "../../src/auth/session-policy.js";

test("同日到期的精确边界视为已过期", () => {
  const session = { expiresAt: "2026-09-11T23:59:59.999Z" };
  const now = new Date("2026-09-11T23:59:59.999Z");

  assert.equal(isSessionExpired(session, now), true);
});