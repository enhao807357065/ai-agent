import { describe, expect, it } from "vitest";
import { loadConfig } from "../../src/config.js";
import { createGatewayProvider } from "../../src/pi/provider.js";

describe("模型接入配置", () => {
  it("默认使用自建 Gateway 的 chat-default 和 GATEWAY_API_KEY", () => {
    const config = loadConfig("config/platform.yaml", undefined, "gateway");

    expect(config.model.providerId).toBe("ai-agent-gateway");
    expect(config.model.id).toBe("chat-default");
    expect(config.gateway.baseUrl).toBe("http://127.0.0.1:8000/v1");
    expect(config.gateway.apiKeyEnv).toBe("GATEWAY_API_KEY");
  });

  it("选择 deepseek 时改为直连 DeepSeek 原生 Chat Completions API", () => {
    const config = loadConfig("config/platform.yaml", undefined, "deepseek");
    const provider = createGatewayProvider(config) as unknown as { name: string; baseUrl: string; getModels(): Array<Record<string, unknown>> };
    const model = provider.getModels()[0];

    expect(provider.name).toBe("DeepSeek");
    expect(provider.baseUrl).toBe("https://api.deepseek.com");
    expect(model).toMatchObject({ provider: "deepseek", id: "deepseek-v4-flash", reasoning: true });
    expect(model.compat).toMatchObject({
      thinkingFormat: "deepseek",
      requiresReasoningContentOnAssistantMessages: true,
    });
  });

  it("未知 provider 在装配前显式失败", () => {
    expect(() => loadConfig("config/platform.yaml", undefined, "not-a-provider" as never)).toThrow(/MODEL_PROVIDER_UNKNOWN/);
  });
});
