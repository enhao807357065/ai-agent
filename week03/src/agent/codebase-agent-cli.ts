import { contentText } from "@earendil-works/pi-ai";
import { createGatewayModelRuntime } from "../model/gateway-models.js";
import { installSigintCancellation } from "./cli-cancellation.js";
import { runCodebaseAgent } from "./codebase-agent-run.js";

const modelRuntime = createGatewayModelRuntime();
const controller = new AbortController();
const removeSigintCancellation = installSigintCancellation(controller);

// Ctrl+C 只触发标准 abort；runCodebaseAgent 将它传给 Pi、模型流和 ToolRuntime。
const result = await runCodebaseAgent({
  model: modelRuntime.model,
  streamFn: (model, context, options) => modelRuntime.stream(context, options),
  signal: controller.signal,
  onTextDelta: (text) => process.stdout.write(text),
}).finally(removeSigintCancellation);

process.stdout.write("\n\n");
console.log(JSON.stringify({
  runId: result.state.runId,
  turns: result.state.turnCount,
  stopReason: result.state.stopReason,
  readEvidence: result.state.stopCenter.readSourceFiles,
  artifactEvidence: result.state.stopCenter.writtenArtifacts,
  trace: result.trace,
}, null, 2));

const finalAssistant = [...result.messages].reverse().find((message) => message.role === "assistant");
if (finalAssistant?.role === "assistant") {
  const visibleText = contentText(finalAssistant.content);
  if (visibleText.length > 0) console.log(`\nFinal:\n${visibleText}`);
}
