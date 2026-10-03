import assert from "node:assert/strict";
import test from "node:test";
import { issueAccessToken, parseAccessToken } from "../src/utils/token";

const identity = { userId: "u-1", role: "member" as const };
const issuedAt = 1_000;
const expiresAt = 2_000;
const token = issueAccessToken(identity, issuedAt, expiresAt - issuedAt);

test("access token remains valid immediately before expiresAt", () => {
  const parsed = parseAccessToken(token, expiresAt - 1);
  assert.equal(parsed.ok, true);
  if (parsed.ok) assert.equal(parsed.payload.expiresAt, expiresAt);
});

test("access token expires exactly at expiresAt", () => {
  assert.deepEqual(parseAccessToken(token, expiresAt), { ok: false, reason: "expired" });
});

test("access token remains expired after expiresAt", () => {
  assert.deepEqual(parseAccessToken(token, expiresAt + 1), { ok: false, reason: "expired" });
});

test("legacy token without time fields is invalid instead of becoming non-expiring", () => {
  const legacy = `demo-token.${Buffer.from(JSON.stringify(identity), "utf8").toString("base64url")}`;
  assert.deepEqual(parseAccessToken(legacy, issuedAt), { ok: false, reason: "invalid" });
});
