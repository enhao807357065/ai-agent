import { describe, expect, it } from "vitest";
import { AuthService } from "../../src/services/auth-service";

describe("AuthService.login", () => {
  it("returns a token and public user data for valid credentials", async () => {
    const service = new AuthService();

    await expect(service.login("demo@example.com", "demo123")).resolves.toEqual({
      accessToken: "demo-token.eyJ1c2VySWQiOiJ1XzEwMDEiLCJyb2xlIjoibWVtYmVyIn0",
      user: {
        id: "u_1001",
        email: "demo@example.com",
        displayName: "Demo User",
        role: "member"
      }
    });
  });

  it("rejects an unknown email address", async () => {
    const service = new AuthService();

    await expect(service.login("missing@example.com", "any-password")).rejects.toThrow(
      "User not found"
    );
  });

  it("rejects an invalid password", async () => {
    const service = new AuthService();

    await expect(service.login("demo@example.com", "wrong-password")).rejects.toThrow(
      "Invalid password"
    );
  });
});
