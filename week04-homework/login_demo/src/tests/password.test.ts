import { describe, expect, it } from "vitest";
import { verifyPassword } from "../utils/password";

describe("verifyPassword", () => {
  it("returns true when the supplied password matches the demo hash", async () => {
    await expect(verifyPassword("demo123", "hash:demo123")).resolves.toBe(true);
  });

  it("returns false when the supplied password does not match the demo hash", async () => {
    await expect(verifyPassword("wrong-password", "hash:demo123")).resolves.toBe(false);
  });
});
