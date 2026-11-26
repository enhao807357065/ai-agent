import type { PlatformStore } from "../store.js";

/**
 * 轨迹图的四类调用：整个会话、一轮、一次模型请求、一次工具执行；审批等待单独记一类。
 * 只加这几种，是因为轨迹图要回答的问题只有"这段时间花在谁身上"。
 */
export type SpanKind = "run" | "turn" | "model" | "tool" | "approval" | "plan";

export type SpanStatus =
  | "running"
  | "ok"
  | "error"
  | "blocked"
  | "rejected"
  | "waiting"
  | "approved";

export interface SpanHandle {
  id: string;
  startMs: number;
}

/**
 * 把"何时开始、何时结束"钉在进程内真实时钟上。
 * 轨迹图不做时间估算：bar 的长度就是这段代码实际占用的墙钟时间。
 */
export class SpanRecorder {
  constructor(
    private readonly store: PlatformStore,
    private readonly traceId: string,
    private readonly runId: string,
  ) {}

  start(
    kind: SpanKind,
    name: string,
    input: { parentId?: string; turnIndex?: number; payload?: Record<string, unknown>; startMs?: number } = {},
  ): SpanHandle {
    const startMs = input.startMs ?? Date.now();
    const id = this.store.startSpan({
      traceId: this.traceId,
      runId: this.runId,
      parentId: input.parentId,
      kind,
      name,
      turnIndex: input.turnIndex,
      startMs,
      payload: input.payload,
    });
    return { id, startMs };
  }

  end(handle: SpanHandle, status: SpanStatus, payload?: Record<string, unknown>, error?: unknown): void {
    this.store.endSpan(handle.id, { status, payload, error });
  }

  fail(handle: SpanHandle, status: SpanStatus, error: unknown, payload?: Record<string, unknown>): void {
    this.store.endSpan(handle.id, { status, payload, error: describeError(error) });
  }

  /** 一次瞬时调用（例如被门口拦下、没有真正执行的工具）也要在图上占一行。 */
  point(
    kind: SpanKind,
    name: string,
    status: SpanStatus,
    input: { parentId?: string; turnIndex?: number; payload?: Record<string, unknown>; error?: unknown } = {},
  ): void {
    const now = Date.now();
    const handle = this.start(kind, name, { ...input, startMs: now });
    this.store.endSpan(handle.id, {
      status,
      endMs: now,
      payload: input.payload,
      error: input.error === undefined ? undefined : describeError(input.error),
    });
  }
}

export function describeError(error: unknown): { message: string; code?: string } {
  if (error instanceof Error) {
    const code = (error as Error & { code?: unknown }).code;
    return { message: error.message, ...(typeof code === "string" ? { code } : {}) };
  }
  if (typeof error === "string") return { message: error };
  return { message: safeStringify(error) };
}

/** 模型与工具的输出都可能很大；图上只需要能认出"这是哪次调用的结果"。 */
export function previewText(value: unknown, max = 400): string {
  const text = typeof value === "string" ? value : safeStringify(value);
  const oneLine = text.replace(/\s+/g, " ").trim();
  return oneLine.length <= max ? oneLine : `${oneLine.slice(0, max)}…`;
}

export function argsPreview(args: Record<string, unknown> | undefined, max = 240): string {
  if (!args) return "";
  const parts = Object.entries(args).map(([key, value]) => {
    const rendered = typeof value === "string" ? previewText(value, 120) : safeStringify(value);
    return `${key}=${rendered.length > 120 ? `${rendered.slice(0, 120)}…` : rendered}`;
  });
  const joined = parts.join(", ");
  return joined.length <= max ? joined : `${joined.slice(0, max)}…`;
}

function safeStringify(value: unknown): string {
  if (value === undefined) return "";
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}
