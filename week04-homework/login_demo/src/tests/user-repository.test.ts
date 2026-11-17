import { describe, expect, it } from "vitest";
import { userRepository } from "../repositories/user-repository";

describe("UserRepository.findByEmail", () => {
  it("returns the matching user record", async () => {
    await expect(userRepository.findByEmail("demo@example.com")).resolves.toMatchObject({
      id: "u_1001",
      email: "demo@example.com",
      role: "member"
    });
  });

  it("returns undefined when no user has the email", async () => {
    await expect(userRepository.findByEmail("missing@example.com")).resolves.toBeUndefined();
  });
});
