import {
  createAssistantMessageEventStream,
  type AssistantMessage,
  type Context,
  type Model,
} from "@earendil-works/pi-ai";
import type { StreamFn } from "@earendil-works/pi-agent-core";

const usage = {
  input: 0,
  output: 0,
  cacheRead: 0,
  cacheWrite: 0,
  totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

export type ScriptedResponse =
  | { readonly kind: "final"; readonly text: string }
  | { readonly kind: "tool"; readonly id: string; readonly name: string; readonly arguments: Record<string, unknown> }
  | { readonly kind: "aborted" }
  | { readonly kind: "error"; readonly message: string };

export function createScriptedStream(
  responses: readonly ScriptedResponse[],
  observedContexts: Context[] = [],
): StreamFn {
  let index = 0;
  return (model, context, options) => {
    observedContexts.push(context);
    const response = responses[index++];
    if (response === undefined) {
      return finishedStream(model, { kind: "final", text: "No more scripted responses." });
    }
    if (response.kind === "aborted") {
      return abortedStream(model, options?.signal);
    }
    return finishedStream(model, response);
  };
}

function finishedStream(model: Model<any>, response: Exclude<ScriptedResponse, { kind: "aborted" }>) {
  const stream = createAssistantMessageEventStream();
  const message = assistantMessage(model, response);
  queueMicrotask(() => {
    stream.push({ type: "start", partial: message });
    if (response.kind === "final" && response.text.length > 0) {
      stream.push({ type: "text_delta", contentIndex: 0, delta: response.text, partial: message });
    }
    if (response.kind === "error") {
      stream.push({ type: "error", reason: "error", error: message });
    } else {
      stream.push({ type: "done", reason: message.stopReason as "stop" | "toolUse", message });
    }
    stream.end(message);
  });
  return stream;
}

function abortedStream(model: Model<any>, signal: AbortSignal | undefined) {
  const stream = createAssistantMessageEventStream();
  const emitAborted = () => {
    const message = assistantMessage(model, { kind: "aborted" });
    stream.push({ type: "error", reason: "aborted", error: message });
    stream.end(message);
  };
  if (signal?.aborted) queueMicrotask(emitAborted);
  else signal?.addEventListener("abort", emitAborted, { once: true });
  return stream;
}

function assistantMessage(model: Model<any>, response: ScriptedResponse): AssistantMessage {
  if (response.kind === "tool") {
    return {
      role: "assistant",
      api: model.api,
      provider: model.provider,
      model: model.id,
      content: [{ type: "toolCall", id: response.id, name: response.name, arguments: response.arguments }],
      usage,
      stopReason: "toolUse",
      timestamp: Date.now(),
    };
  }
  if (response.kind === "error") {
    return {
      role: "assistant",
      api: model.api,
      provider: model.provider,
      model: model.id,
      content: [{ type: "text", text: "" }],
      usage,
      stopReason: "error",
      errorMessage: response.message,
      timestamp: Date.now(),
    };
  }
  if (response.kind === "aborted") {
    return {
      role: "assistant",
      api: model.api,
      provider: model.provider,
      model: model.id,
      content: [],
      usage,
      stopReason: "aborted",
      timestamp: Date.now(),
    };
  }
  return {
    role: "assistant",
    api: model.api,
    provider: model.provider,
    model: model.id,
    content: [{ type: "text", text: response.text }],
    usage,
    stopReason: "stop",
    timestamp: Date.now(),
  };
}
