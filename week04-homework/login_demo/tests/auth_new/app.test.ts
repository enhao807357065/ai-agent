import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { afterEach, describe, expect, it } from "vitest";
import { createApp } from "../../src/app";

const servers: Server[] = [];

async function startApp(): Promise<string> {
  const server = createApp().listen(0);
  servers.push(server);

  await new Promise<void>((resolve) => server.once("listening", resolve));

  const { port } = server.address() as AddressInfo;
  return `http://127.0.0.1:${port}`;
}

afterEach(async () => {
  await Promise.all(
    servers.splice(0).map(
      (server) => new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())))
    )
  );
});

describe("login application HTTP flow", () => {
  it("returns health status", async () => {
    const baseUrl = await startApp();

    const response = await fetch(`${baseUrl}/health`);

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ ok: true });
  });

  it("logs in with valid credentials and returns public user data", async () => {
    const baseUrl = await startApp();

    const response = await fetch(`${baseUrl}/api/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email: "demo@example.com", password: "demo123" })
    });

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      accessToken: "demo-token.eyJ1c2VySWQiOiJ1XzEwMDEiLCJyb2xlIjoibWVtYmVyIn0",
      user: {
        id: "u_1001",
        email: "demo@example.com",
        displayName: "Demo User",
        role: "member"
      }
    });
  });

  it("rejects a login request without required credentials", async () => {
    const baseUrl = await startApp();

    const response = await fetch(`${baseUrl}/api/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email: "demo@example.com" })
    });

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({ error: "email and password are required" });
  });

  it("rejects invalid credentials", async () => {
    const baseUrl = await startApp();

    const response = await fetch(`${baseUrl}/api/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email: "demo@example.com", password: "wrong-password" })
    });

    expect(response.status).toBe(401);
    await expect(response.json()).resolves.toEqual({ error: "Invalid password" });
  });

  it("protects the profile route and accepts a valid bearer token", async () => {
    const baseUrl = await startApp();

    const unauthorized = await fetch(`${baseUrl}/api/profile`);
    expect(unauthorized.status).toBe(401);
    await expect(unauthorized.json()).resolves.toEqual({ error: "Missing bearer token" });

    const authorized = await fetch(`${baseUrl}/api/profile`, {
      headers: {
        authorization: "Bearer demo-token.eyJ1c2VySWQiOiJ1XzEwMDEiLCJyb2xlIjoibWVtYmVyIn0"
      }
    });

    expect(authorized.status).toBe(200);
    await expect(authorized.json()).resolves.toEqual({
      message: "Authenticated request",
      currentUser: { userId: "u_1001", role: "member" }
    });
  });
});
