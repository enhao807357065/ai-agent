import { describe, expect, it } from "vitest";
import { loadConfig } from "../../src/config.js";
import { selectDecider } from "../../src/pi/decider.js";
import { createGatewayProvider } from "../../src/pi/provider.js";

describe("AI Agent Gateway provider", () => {
  it("默认以 Gateway 自己的凭据变量决定 real 决策器能否连接", () => {
    const config = loadConfig("config/platform.yaml");

    expect(config.gateway.baseUrl).toBe("http://127.0.0.1:8000/v1");
    expect(config.gateway.apiKeyEnv).toBe("GATEWAY_API_KEY");
    expect(selectDecider(config, { requested: "real", hasApiKey: true }).selection.kind).toBe("gateway");
  });

  it("注册的是中性的 OpenAI Chat Completions Gateway，而不是 DeepSeek 专属 compat", () => {
    const config = loadConfig("config/platform.yaml");
    const provider = createGatewayProvider(config) as unknown as {
      id: string;
      name: string;
      baseUrl: string;
      getModels(): Array<Record<string, unknown>>;
    };
    const model = provider.getModels()[0];

    expect(provider.name).toBe("AI Agent Gateway");
    expect(provider.id).toBe("ai-agent-gateway");
    expect(provider.baseUrl).toBe("http://127.0.0.1:8000/v1");
    expect(model).toMatchObject({
      provider: "ai-agent-gateway",
      api: "openai-completions",
      baseUrl: "http://127.0.0.1:8000/v1",
    });
    expect(model.reasoning).toBe(false);
    expect(model.compat).not.toMatchObject({ thinkingFormat: "deepseek" });
  });

  it("Gateway 路径运行时不会下发 DeepSeek 专属 compat", () => {
    const config = loadConfig("config/platform.yaml", undefined, "gateway");
    const provider = createGatewayProvider(config) as unknown as { getModels(): Array<Record<string, unknown>> };
    const model = provider.getModels()[0];

    expect(model.reasoning).toBe(false);
    expect(model.compat).not.toMatchObject({ thinkingFormat: "deepseek" });
    expect(model.compat).not.toMatchObject({ requiresReasoningContentOnAssistantMessages: true });
  });
});
