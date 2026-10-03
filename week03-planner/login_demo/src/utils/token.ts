import { AuthTokenPayload, ParsedAccessToken } from "../types/user";

export const DEFAULT_ACCESS_TOKEN_TTL_MS = 30 * 60 * 1000;

type TokenIdentity = Pick<AuthTokenPayload, "userId" | "role">;

/** 签发时间由服务端提供；测试可传固定 now，从而稳定覆盖过期临界点。 */
export function issueAccessToken(
  identity: TokenIdentity,
  now: number = Date.now(),
  ttlMs: number = DEFAULT_ACCESS_TOKEN_TTL_MS,
): string {
  const payload: AuthTokenPayload = {
    ...identity,
    issuedAt: now,
    expiresAt: now + ttlMs,
  };
  const encoded = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
  return `demo-token.${encoded}`;
}

/**
 * 有效区间为 [issuedAt, expiresAt)。
 * 因而 now === expiresAt 必须返回 expired，而不是继续放行一毫秒。
 */
export function parseAccessToken(token: string, now: number = Date.now()): ParsedAccessToken {
  if (!token.startsWith("demo-token.")) return { ok: false, reason: "invalid" };

  try {
    const encoded = token.replace("demo-token.", "");
    const payload = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8")) as Partial<AuthTokenPayload>;
    if (
      typeof payload.userId !== "string" ||
      (payload.role !== "admin" && payload.role !== "member") ||
      typeof payload.issuedAt !== "number" ||
      typeof payload.expiresAt !== "number" ||
      !Number.isFinite(payload.issuedAt) ||
      !Number.isFinite(payload.expiresAt) ||
      payload.expiresAt <= payload.issuedAt
    ) return { ok: false, reason: "invalid" };

    if (now >= payload.expiresAt) return { ok: false, reason: "expired" };
    return { ok: true, payload: payload as AuthTokenPayload };
  } catch {
    return { ok: false, reason: "invalid" };
  }
}
