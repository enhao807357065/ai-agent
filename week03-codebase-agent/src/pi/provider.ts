import {
  createProvider,
  envApiKeyAuth,
  type Model,
} from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/compat";
import type { PlatformConfig } from "../config.js";

export function createGatewayProvider(config: PlatformConfig) {
  const isDeepSeek = config.model.transport === "deepseek";
  const model: Model<"openai-completions"> = {
    id: config.model.id,
    name: config.model.name,
    api: "openai-completions",
    provider: config.model.providerId,
    baseUrl: config.gateway.baseUrl,
    // Gateway 对外只承诺 OpenAI Chat Completions；不假定上游私有 reasoning 字段。
    // 仅当用户显式选择 deepseek 直连时，才启用 DeepSeek 的回放兼容规则。
    reasoning: isDeepSeek,
    compat: {
      supportsStore: false,
      supportsDeveloperRole: false,
      maxTokensField: "max_tokens",
      supportsStrictMode: true,
      ...(isDeepSeek
        ? {
            requiresReasoningContentOnAssistantMessages: true,
            thinkingFormat: "deepseek" as const,
          }
        : {}),
    },
    input: ["text"],
    cost: {
      input: config.model.inputPerMillion,
      output: config.model.outputPerMillion,
      cacheRead: config.model.cacheReadPerMillion,
      cacheWrite: config.model.inputPerMillion,
    },
    contextWindow: config.model.contextWindow,
    maxTokens: config.model.maxTokens,
  };
  return createProvider({
    id: config.model.providerId,
    name: isDeepSeek ? "DeepSeek" : "AI Agent Gateway",
    baseUrl: config.gateway.baseUrl,
    auth: {
      apiKey: envApiKeyAuth(isDeepSeek ? "DeepSeek API key" : "AI Agent Gateway API key", [config.gateway.apiKeyEnv]),
    },
    models: [model],
    api: openAICompletionsApi(),
  });
}
