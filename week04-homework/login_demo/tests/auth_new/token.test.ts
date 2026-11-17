import { describe, expect, it } from "vitest";
import { issueAccessToken, parseAccessToken } from "../../src/utils/token";

describe("access token helpers", () => {
  it("round-trips a payload issued by the application", () => {
    const payload = { userId: "u_1001", role: "member" as const };

    const token = issueAccessToken(payload);

    expect(token).toMatch(/^demo-token\./);
    expect(parseAccessToken(token)).toEqual(payload);
  });

  it("rejects a token with an unknown prefix", () => {
    expect(parseAccessToken("Bearer not-a-demo-token")).toBeNull();
  });

  it("rejects a malformed token payload", () => {
    expect(parseAccessToken("demo-token.not-base64-json")).toBeNull();
  });
});
