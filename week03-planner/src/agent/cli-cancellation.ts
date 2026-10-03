/**
 * 将终端 SIGINT（Ctrl+C）转换为同一个 Run 的 AbortSignal。
 *
 * 取消的实际传播由装配层完成：signal → Pi runAgentLoop → 模型流与 ToolRuntime。
 * 此模块不决定停止原因，也不直接终止进程；它只把 CLI 输入转为标准取消协议。
 */
export function installSigintCancellation(
  controller: AbortController,
  processLike: Pick<typeof process, "once" | "removeListener"> = process,
): () => void {
  const abortRun = (): void => controller.abort();

  // once：第一次 Ctrl+C 请求优雅取消；处理器触发后自动移除，第二次交由 Node 默认行为处理。
  processLike.once("SIGINT", abortRun);

  // 正常结束、模型错误或其他异常时主动移除尚未触发的监听器，避免 CLI 生命周期泄漏。
  return () => processLike.removeListener("SIGINT", abortRun);
}
