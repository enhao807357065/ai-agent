import assert from "node:assert/strict";
import test from "node:test";
import type { NextFunction, Request, Response } from "express";
import { authMiddleware } from "../src/middleware/auth-middleware";
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

/**
 * 会话到期在 HTTP 层的判定点在 authMiddleware，它依赖 parseAccessToken 的默认 now=Date.now()。
 * 通过临时冻结 Date.now 让中间件边界可稳定复现，避免真实时钟导致的偶发失败。
 */
function withFrozenNow<T>(now: number, run: () => T): T {
  const originalNow = Date.now;
  Date.now = () => now;
  try {
    return run();
  } finally {
    Date.now = originalNow;
  }
}

function createAuthHarness() {
  const state = {
    statusCode: undefined as number | undefined,
    body: undefined as unknown,
    nextCalled: false,
    locals: {} as Record<string, unknown>,
  };
  const request = {
    headers: {} as Record<string, string | undefined>,
  } as unknown as Request;
  const response = {
    locals: state.locals,
    status(code: number) {
      state.statusCode = code;
      return response;
    },
    json(payload: unknown) {
      state.body = payload;
    },
  } as unknown as Response;
  const next: NextFunction = () => {
    state.nextCalled = true;
  };
  return { state, request, response, next };
}

test("auth middleware rejects a session exactly at expiresAt with 401 Token expired", () => {
  const { state, request, response, next } = createAuthHarness();
  request.headers.authorization = `Bearer ${token}`;
  withFrozenNow(expiresAt, () => authMiddleware(request, response, next));
  assert.equal(state.statusCode, 401);
  assert.deepEqual(state.body, { error: "Token expired" });
  assert.equal(state.nextCalled, false);
});

test("auth middleware still allows the session one millisecond before expiresAt", () => {
  const { state, request, response, next } = createAuthHarness();
  request.headers.authorization = `Bearer ${token}`;
  withFrozenNow(expiresAt - 1, () => authMiddleware(request, response, next));
  assert.equal(state.statusCode, undefined);
  assert.equal(state.nextCalled, true);
  assert.deepEqual(state.locals.currentUser, {
    userId: identity.userId,
    role: identity.role,
    issuedAt,
    expiresAt,
  });
});

test("auth middleware keeps rejecting an expired session after expiresAt", () => {
  const { state, request, response, next } = createAuthHarness();
  request.headers.authorization = `Bearer ${token}`;
  withFrozenNow(expiresAt + 1, () => authMiddleware(request, response, next));
  assert.equal(state.statusCode, 401);
  assert.deepEqual(state.body, { error: "Token expired" });
  assert.equal(state.nextCalled, false);
});

test("auth middleware maps a non-expiry failure to 401 Invalid token", () => {
  const { state, request, response, next } = createAuthHarness();
  request.headers.authorization = "Bearer not-a-demo-token";
  withFrozenNow(expiresAt, () => authMiddleware(request, response, next));
  assert.equal(state.statusCode, 401);
  assert.deepEqual(state.body, { error: "Invalid token" });
  assert.equal(state.nextCalled, false);
});
