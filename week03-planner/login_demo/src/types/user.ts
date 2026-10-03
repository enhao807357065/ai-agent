export interface UserRecord {
  id: string;
  email: string;
  passwordHash: string;
  displayName: string;
  role: "admin" | "member";
}

export interface LoginResult {
  accessToken: string;
  user: Pick<UserRecord, "id" | "email" | "displayName" | "role">;
}

/** Token 的有效区间是 [issuedAt, expiresAt)：到达 expiresAt 的瞬间即失效。 */
export interface AuthTokenPayload {
  userId: string;
  role: UserRecord["role"];
  issuedAt: number;
  expiresAt: number;
}

export type ParsedAccessToken =
  | { ok: true; payload: AuthTokenPayload }
  | { ok: false; reason: "invalid" | "expired" };
