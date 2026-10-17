import { afterEach, describe, expect, it } from "vitest";
import {
  codebaseAgentModel,
  createGatewayModelRuntime,
} from "../src/model/gateway-models.js";

const baseUrlEnvironmentVariable = "CODEBASE_GATEWAY_BASE_URL";
const apiKeyEnvironmentVariable = "CODEBASE_GATEWAY_API_KEY";
const originalBaseUrl = process.env[baseUrlEnvironmentVariable];
const originalApiKey = process.env[apiKeyEnvironmentVariable];

afterEach(() => {
  restoreEnvironment(baseUrlEnvironmentVariable, originalBaseUrl);
  restoreEnvironment(apiKeyEnvironmentVariable, originalApiKey);
});

describe("Gateway model runtime", () => {
  it("registers the logical model before exposing the Models collection", () => {
    const runtime = createGatewayModelRuntime({
      baseUrl: "http://test.invalid/v1",
    });

    expect(runtime.models.getProvider("gateway")).toBeDefined();
    expect(runtime.models.getModel("gateway", codebaseAgentModel.id)).toEqual(
      runtime.model,
    );
    expect(runtime.model).toMatchObject({
      id: "chat-default",
      api: "openai-completions",
      reasoning: false,
      input: ["text"],
      contextWindow: 32_768,
      maxTokens: 8_192,
      cost: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
      },
    });
    expect(typeof runtime.stream).toBe("function");
  });

  it("reads the endpoint and credential from environment variables", async () => {
    process.env[baseUrlEnvironmentVariable] = "http://environment.invalid/v1";
    process.env[apiKeyEnvironmentVariable] = "test-only";

    const runtime = createGatewayModelRuntime();
    const auth = await runtime.models.getAuth(runtime.model);

    expect(runtime.model.baseUrl).toBe("http://environment.invalid/v1");
    expect(auth?.auth.apiKey).toBe("test-only");
  });

  it("uses the local gateway address when no endpoint override is set", () => {
    delete process.env[baseUrlEnvironmentVariable];

    const runtime = createGatewayModelRuntime();

    expect(runtime.model.baseUrl).toBe("http://localhost:8000/v1");
  });
});

function restoreEnvironment(name: string, value: string | undefined): void {
  if (value === undefined) {
    delete process.env[name];
    return;
  }

  process.env[name] = value;
}
