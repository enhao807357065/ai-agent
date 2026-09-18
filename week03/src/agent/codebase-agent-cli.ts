import { contentText } from "@earendil-works/pi-ai";
import { createGatewayModelRuntime } from "../model/gateway-models.js";
import { runCodebaseAgent } from "./codebase-agent-run.js";

const modelRuntime = createGatewayModelRuntime();
const result = await runCodebaseAgent({
  model: modelRuntime.model,
  streamFn: (model, context, options) => modelRuntime.stream(context, options),
  onTextDelta: (text) => process.stdout.write(text),
});

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
