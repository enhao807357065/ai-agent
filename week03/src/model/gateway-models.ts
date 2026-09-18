import {
  createModels,
  createProvider,
  envApiKeyAuth,
  lazyApi,
  type AssistantMessageEventStream,
  type Context,
  type Model,
  type Models,
  type StreamOptions,
} from "@earendil-works/pi-ai";
import type { ProviderStreams } from "@earendil-works/pi-ai";

type GatewayPiModel = Model<"openai-completions">;

const gatewayProviderId = "gateway";
const logicalModelId = "chat-default";
const defaultGatewayBaseUrl = "http://localhost:8000/v1";
const gatewayBaseUrlEnvironmentVariable = "CODEBASE_GATEWAY_BASE_URL";
const gatewayApiKeyEnvironmentVariable = "CODEBASE_GATEWAY_API_KEY";

export const codebaseAgentModel: GatewayPiModel = {
  id: logicalModelId,
  name: "Codebase Agent",
  api: "openai-completions",
  provider: gatewayProviderId,
  baseUrl: defaultGatewayBaseUrl,
  reasoning: false,
  input: ["text"],
  cost: {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
  },
  contextWindow: 32_768,
  maxTokens: 8_192,
  compat: {
    supportsReasoningEffort: false,
  },
};

export interface GatewayModelOptions {
  readonly baseUrl?: string;
}

export interface GatewayModelRuntime {
  readonly model: GatewayPiModel;
  readonly models: Models;
  stream(
    context: Context,
    options?: StreamOptions,
  ): AssistantMessageEventStream;
}

export function createGatewayModelRuntime(
  options: GatewayModelOptions = {},
): GatewayModelRuntime {
  const baseUrl = options.baseUrl ?? readGatewayBaseUrl();
  const apiKey = process.env[gatewayApiKeyEnvironmentVariable];
  const model: GatewayPiModel = { ...codebaseAgentModel, baseUrl };

  const provider = createProvider({
    id: gatewayProviderId,
    name: "Gateway",
    baseUrl,
    auth: {
      apiKey: envApiKeyAuth("Gateway API key", [gatewayApiKeyEnvironmentVariable]),
    },
    models: [model],
    api: gatewayOpenAiCompletionsApi,
  });

  const models = createModels({
    authContext: {
      env: async (name) =>
        name === gatewayApiKeyEnvironmentVariable ? apiKey : process.env[name],
      fileExists: async () => false,
    },
  });

  models.setProvider(provider);

  return Object.freeze({
    model,
    models,
    stream: (
      context: Context,
      streamOptions?: StreamOptions,
    ): AssistantMessageEventStream =>
      models.stream(model, context, streamOptions),
  });
}

const gatewayOpenAiCompletionsApi = lazyApi(
  async (): Promise<ProviderStreams> => {
    const moduleName = "@earendil-works/pi-ai/api/openai-completions.lazy";
    const api = await import(moduleName) as {
      openAICompletionsApi(): ProviderStreams;
    };

    return api.openAICompletionsApi();
  },
);

function readGatewayBaseUrl(): string {
  return process.env[gatewayBaseUrlEnvironmentVariable] ?? defaultGatewayBaseUrl;
}
