import type { PlatformStore, TraceSpanRecord } from "../store.js";

/**
 * 轨迹图 DTO。collector 只做"把库里的事实排成一条时间轴"，不做任何耗时估算：
 * 每个节点的 start/end 都来自 span 写入时的真实时钟。
 */
export interface TraceNode {
  id: string;
  parentId: string | null;
  kind: string;
  name: string;
  status: string;
  turnIndex: number | null;
  startMs: number;
  endMs: number;
  durationMs: number;
  offsetMs: number;
  depth: number;
  payload: Record<string, unknown>;
  error: { message?: string; code?: string } | null;
  /** 来自 approvals 表的人工等待节点：它不在 trace_spans 里，但属于同一条时间轴。 */
  derived?: "approval";
  /** 轮次行画的是"本轮及其子调用的外包络"，不是 turn span 本身；两者可能差几毫秒。 */
  bandStartMs?: number;
  bandEndMs?: number;
}

export interface TraceTurn {
  index: number;
  startMs: number;
  endMs: number;
  durationMs: number;
  modelMs: number;
  toolMs: number;
  toolCalls: number;
  status: string;
}

export interface TraceStats {
  turns: number;
  modelCalls: number;
  toolCalls: number;
  /** 审批门槛 span 数（走了一次审批判定），不等于"人真的审了几次"。 */
  approvals: number;
  approvalGates: number;
  /** 真的产生了人工等待的审批单数（来自 approvals 表派生的节点，已剔除自动审批）。 */
  humanApprovals: number;
  /** 由 --auto-approve 身份立即答掉的单数。单子照开，只是没人真的等。 */
  autoApprovals: number;
  errors: number;
  modelMs: number;
  toolMs: number;
  approvalWaitMs: number;
  spanCount: number;
  status: string;
  acceptance: string | null;
  stop: string | null;
}

export interface TraceView {
  traceId: string;
  runId: string | null;
  title: string;
  task: string | null;
  model: string | null;
  decider: string | null;
  prompt: string | null;
  workspace: string | null;
  startedAt: string;
  endedAt: string;
  totalMs: number;
  stats: TraceStats;
  turns: TraceTurn[];
  nodes: TraceNode[];
  /** 有 span 之前的旧会话：界面要明确说"这条对话没有采集到时间轴"，而不是画一张空图。 */
  legacy: boolean;
}

export function collectTrace(store: PlatformStore, traceId: string): TraceView {
  const rows = store.listSpans(traceId);
  const run = store.getRunByTrace(traceId);
  const runPayload = run ? safeJson(run.payload_json as string) : {};

  if (rows.length === 0) {
    return legacyView(traceId, run, runPayload);
  }

  const nodes: TraceNode[] = rows.map((row) => toNode(row));

  // 审批门槛 span 收尾时未必拿得到单号（单子是后来落库的），按 tool_call_id 从 approvals 表补齐，
  // 否则详情面板"审批单"是空的，而错误文本里却有单号，读起来自相矛盾。
  const approvalsForTrace = store.listApprovalsByTrace(traceId);
  // 自动审批与人工审批在 approvals 表里长得一样（都是 approved + decided_by），
  // 区别只在 hitl.approved 事件的 auto 标记；读图时必须能分开，否则会以为是人在等。
  const autoApprovalIds = new Set(
    (store.getTrace(traceId) as Array<{ event_type: string; payload_json: string }>)
      .filter((event) => event.event_type === "hitl.approved")
      .map((event) => safeJson(event.payload_json))
      .filter((payload) => payload.auto === true && payload.approval_id)
      .map((payload) => String(payload.approval_id)),
  );
  for (const node of nodes) {
    if (node.kind !== "approval") continue;
    const match = approvalsForTrace.find((approval) => approval.tool_call_id === node.payload.tool_call_id);
    if (!match) continue;
    node.payload = {
      ...node.payload,
      approval_id: match.id,
      approval_status: match.status,
      decided_by: match.decided_by ?? null,
      decided_at: match.decided_at ?? null,
      consumed_at: match.consumed_at ?? null,
      args_digest: match.args_digest ?? null,
    };
  }
  const rootRow = rows.find((row) => row.kind === "run" && !row.parent_id) ?? rows[0];
  const rootNode = nodes.find((node) => node.id === rootRow.id)!;

  // 同一次工具调用可能留下两条记录：真正执行了但失败的那条，和 beforeToolCall 补记的"被拦"标记。
  // 图上一次调用只该占一行，否则读起来像调了两次；优先保留真正执行过的那条。
  const executedToolCallIds = new Set(
    nodes
      .filter((node) => node.kind === "tool" && node.payload.executed !== false && node.payload.tool_call_id)
      .map((node) => String(node.payload.tool_call_id)),
  );
  const deduped = nodes.filter(
    (node) => !(node.kind === "tool" && node.payload.executed === false && executedToolCallIds.has(String(node.payload.tool_call_id))),
  );
  nodes.length = 0;
  nodes.push(...deduped);

  // 人工审批发生在两个进程之间（Agent 暂停、人答复、再 resume），只能用 approvals 表的时间戳补进时间轴。
  for (const approval of approvalsForTrace) {
    // 审批门槛 span 已经记录了同一张单子；只有真的"等人"（可感知的等待）才值得再画一条。
    const gateCovered = nodes.some(
      (node) => node.kind === "approval" && node.payload.tool_call_id === approval.tool_call_id,
    );
    const created = Date.parse(approval.created_at);
    const decided = approval.decided_at ? Date.parse(approval.decided_at) : null;
    const consumed = approval.consumed_at ? Date.parse(approval.consumed_at) : null;
    const end = consumed ?? decided ?? Date.now();
    if (gateCovered && end - created < 50) continue;
    const parent = nodes.find(
      (node) => node.kind === "tool" && node.payload.tool_call_id === approval.tool_call_id,
    );
    const auto = autoApprovalIds.has(approval.id);
    nodes.push({
      id: `approval:${approval.id}`,
      parentId: parent?.id ?? rootNode.id,
      kind: "approval",
      name: `${auto ? "自动审批" : "人工审批"} · ${approval.tool_name}`,
      status: approvalStatus(approval.status),
      turnIndex: parent?.turnIndex ?? null,
      startMs: created,
      endMs: Math.max(end, created),
      durationMs: Math.max(0, end - created),
      offsetMs: 0,
      depth: 0,
      payload: {
        approval_id: approval.id,
        tool_call_id: approval.tool_call_id,
        tool: approval.tool_name,
        status: approval.status,
        auto,
        decided_by: approval.decided_by ?? null,
        decided_at: approval.decided_at ?? null,
        consumed_at: approval.consumed_at ?? null,
        args_digest: approval.args_digest ?? null,
        error_code: approval.error_code ?? null,
      },
      error: approval.error_code ? { code: approval.error_code } : null,
      derived: "approval",
    });
  }

  const origin = Math.min(...nodes.map((node) => node.startMs));
  const end = Math.max(...nodes.map((node) => node.endMs));
  for (const node of nodes) node.offsetMs = node.startMs - origin;

  const ordered = orderByTree(nodes, rootNode.id);
  for (const turn of ordered) {
    if (turn.kind !== "turn") continue;
    const children = ordered.filter((node) => isDescendantOf(ordered, node, turn.id) && node.id !== turn.id);
    if (children.length === 0) continue;
    turn.bandStartMs = Math.min(turn.startMs, ...children.map((node) => node.startMs));
    turn.bandEndMs = Math.max(turn.endMs, ...children.map((node) => node.endMs));
  }
  const turns = ordered
    .filter((node) => node.kind === "turn")
    .map((node) => {
      const children = nodes.filter((child) => child.parentId === node.id);
      return {
        index: node.turnIndex ?? 0,
        startMs: node.startMs,
        endMs: node.endMs,
        durationMs: node.durationMs,
        modelMs: sum(children.filter((child) => child.kind === "model").map((child) => child.durationMs)),
        toolMs: sum(children.filter((child) => child.kind === "tool").map((child) => child.durationMs)),
        toolCalls: children.filter((child) => child.kind === "tool").length,
        status: node.status,
      };
    });

  const stats: TraceStats = {
    turns: turns.length,
    modelCalls: nodes.filter((node) => node.kind === "model").length,
    toolCalls: nodes.filter((node) => node.kind === "tool").length,
    approvals: nodes.filter((node) => node.kind === "approval" && node.derived !== "approval").length,
    approvalGates: nodes.filter((node) => node.kind === "approval" && node.derived !== "approval").length,
    humanApprovals: nodes.filter((node) => node.derived === "approval" && node.payload.auto !== true).length,
    autoApprovals: nodes.filter((node) => node.derived === "approval" && node.payload.auto === true).length,
    errors: nodes.filter((node) => ["error", "blocked", "rejected"].includes(node.status)).length,
    modelMs: sum(nodes.filter((node) => node.kind === "model").map((node) => node.durationMs)),
    toolMs: sum(nodes.filter((node) => node.kind === "tool").map((node) => node.durationMs)),
    approvalWaitMs: sum(nodes.filter((node) => node.derived === "approval").map((node) => node.durationMs)),
    spanCount: nodes.length,
    status: rootNode.status,
    acceptance: (rootNode.payload.acceptance as string | undefined) ?? null,
    stop: (rootNode.payload.stop as string | undefined) ?? null,
  };

  return {
    traceId,
    runId: (rootNode.payload.run_id as string | undefined) ?? (run?.id as string | undefined) ?? null,
    title: rootNode.name,
    task: (rootNode.payload.task as string | undefined) ?? null,
    model: (rootNode.payload.model as string | undefined) ?? null,
    decider: (rootNode.payload.decider as string | undefined) ?? null,
    prompt: (rootNode.payload.prompt as string | undefined) ?? null,
    workspace: (rootNode.payload.workspace as string | undefined) ?? null,
    startedAt: new Date(origin).toISOString(),
    endedAt: new Date(end).toISOString(),
    totalMs: Math.max(0, end - origin),
    stats,
    turns,
    nodes: ordered,
    legacy: false,
  };
}

/**
 * 列表页必须和详情页用同一套口径：直接跑 collectTrace，而不是另写一份 SQL 聚合。
 * 否则"列表说 4 个异常、点进去 3 个"——差异来自 collector 按 tool_call_id 去重，SQL 不知道这件事。
 */
export function listTraces(store: PlatformStore): TraceView[] {
  return store
    .listSpanSummaries()
    .sort((a, b) => b.first_ms - a.first_ms)
    .map((summary) => {
      const view = collectTrace(store, summary.trace_id);
      return { ...view, nodes: [], turns: [] } satisfies TraceView;
    });
}

function toNode(row: TraceSpanRecord): TraceNode {
  const startMs = Number(row.start_ms);
  const endMs = row.end_ms == null ? Date.now() : Number(row.end_ms);
  return {
    id: row.id,
    parentId: row.parent_id,
    kind: row.kind,
    name: row.name,
    status: row.status,
    turnIndex: row.turn_index == null ? null : Number(row.turn_index),
    startMs,
    endMs: Math.max(endMs, startMs),
    durationMs: Math.max(0, endMs - startMs),
    offsetMs: 0,
    depth: 0,
    payload: safeJson(row.payload_json),
    error: row.error_json ? safeJson(row.error_json) : null,
  };
}

/** 先序展开成一条"能从上往下读"的列表，深度由 parent 链决定，不靠时间戳猜。 */
function orderByTree(nodes: TraceNode[], rootId: string): TraceNode[] {
  const byParent = new Map<string | null, TraceNode[]>();
  for (const node of nodes) {
    const key = node.parentId;
    byParent.set(key, [...(byParent.get(key) ?? []), node]);
  }
  for (const list of byParent.values()) list.sort((a, b) => a.startMs - b.startMs || a.kind.localeCompare(b.kind));

  const output: TraceNode[] = [];
  const seen = new Set<string>();
  const walk = (node: TraceNode, depth: number): void => {
    if (seen.has(node.id)) return;
    seen.add(node.id);
    node.depth = depth;
    output.push(node);
    for (const child of byParent.get(node.id) ?? []) walk(child, depth + 1);
  };
  const root = nodes.find((node) => node.id === rootId);
  if (root) walk(root, 0);
  // 父节点缺失的孤儿也要露出来，否则数据坏了界面上会静默丢行。
  for (const node of nodes) if (!seen.has(node.id)) walk(node, 0);
  return output;
}

/** 从已排好序的列表里判断祖先：不查表，只顺着 parentId 往上走，遇到孤儿就停。 */
function isDescendantOf(nodes: TraceNode[], node: TraceNode, ancestorId: string): boolean {
  const byId = new Map(nodes.map((item) => [item.id, item]));
  let cursor = node.parentId;
  while (cursor) {
    if (cursor === ancestorId) return true;
    cursor = byId.get(cursor)?.parentId ?? null;
  }
  return false;
}

function approvalStatus(status: string): string {
  if (status === "pending" || status === "executing") return "waiting";
  if (status === "approved") return "approved";
  if (status === "consumed") return "ok";
  if (status === "rejected") return "rejected";
  return "error";
}

function legacyView(
  traceId: string,
  run: Record<string, unknown> | undefined,
  payload: Record<string, unknown>,
): TraceView {
  const startedAt = (run?.created_at as string | undefined) ?? new Date().toISOString();
  return {
    traceId,
    runId: (run?.id as string | undefined) ?? (payload.id as string | undefined) ?? null,
    title: (payload.prompt as string | undefined)?.split("\n")[0] ?? "（旧会话，无时间轴）",
    task: (payload.task as string | undefined) ?? null,
    model: null,
    decider: null,
    prompt: (payload.prompt as string | undefined) ?? null,
    workspace: (run?.workspace as string | undefined) ?? null,
    startedAt,
    endedAt: (run?.updated_at as string | undefined) ?? startedAt,
    totalMs: 0,
    stats: {
      turns: 0,
      modelCalls: 0,
      toolCalls: 0,
      approvals: 0,
      approvalGates: 0,
      humanApprovals: 0,
      autoApprovals: 0,
      errors: 0,
      modelMs: 0,
      toolMs: 0,
      approvalWaitMs: 0,
      spanCount: 0,
      status: (run?.status as string | undefined) ?? "unknown",
      acceptance: null,
      stop: null,
    },
    turns: [],
    nodes: [],
    legacy: true,
  };
}

function safeJson(text: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(text) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0);
}
