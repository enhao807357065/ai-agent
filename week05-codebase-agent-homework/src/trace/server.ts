import { readFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { PlatformStore } from "../store.js";
import { collectTrace, listTraces } from "./collector.js";

export interface TraceServerOptions {
  store: PlatformStore;
  host?: string;
  port?: number;
}

export interface TraceServer {
  url: string;
  host: string;
  port: number;
  close(): Promise<void>;
}

/**
 * 轨迹图服务只做两件事：把 HTML 发出去，把 collector 的结果序列化成 JSON。
 * 不在这里算时间：所有耗时都来自 trace_spans，界面只是画出来。
 */
export async function startTraceServer(options: TraceServerOptions): Promise<TraceServer> {
  const host = options.host ?? "127.0.0.1";
  const html = loadUi();
  const server = createServer((request, response) => {
    handle(request, response, options.store, html).catch((error: unknown) => {
      sendJson(response, 500, { error: error instanceof Error ? error.message : String(error) });
    });
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(options.port ?? 0, host, () => {
      server.off("error", reject);
      resolve();
    });
  });

  const address = server.address();
  const port = typeof address === "object" && address ? address.port : (options.port ?? 0);
  return {
    url: `http://${host}:${port}/`,
    host,
    port,
    close: () => closeServer(server),
  };
}

async function handle(
  request: IncomingMessage,
  response: ServerResponse,
  store: PlatformStore,
  html: string,
): Promise<void> {
  const url = new URL(request.url ?? "/", "http://localhost");
  const pathname = url.pathname;

  if (pathname === "/" || pathname === "/index.html") {
    response.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    response.end(html);
    return;
  }
  if (pathname === "/api/runs") {
    sendJson(response, 200, { runs: listTraces(store) });
    return;
  }
  if (pathname.startsWith("/api/trace/")) {
    const traceId = decodeURIComponent(pathname.slice("/api/trace/".length));
    if (!traceId) {
      sendJson(response, 400, { error: "trace id is required" });
      return;
    }
    const view = collectTrace(store, traceId);
    if (view.stats.spanCount === 0 && !view.legacy && !view.runId) {
      sendJson(response, 404, { error: `trace not found: ${traceId}` });
      return;
    }
    sendJson(response, 200, view);
    return;
  }
  if (pathname === "/healthz") {
    sendJson(response, 200, { ok: true });
    return;
  }
  sendJson(response, 404, { error: `not found: ${pathname}` });
}

function sendJson(response: ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(text),
    "cache-control": "no-store",
  });
  response.end(text);
}

function loadUi(): string {
  // tsx 直跑时 HTML 与源码同目录；编译产物跑时再退回仓库内的 src 目录。
  const candidates = [
    join(dirname(fileURLToPath(import.meta.url)), "ui.html"),
    join(process.cwd(), "src", "trace", "ui.html"),
  ];
  for (const candidate of candidates) {
    try {
      return readFileSync(candidate, "utf8");
    } catch {
      continue;
    }
  }
  throw new Error(`轨迹图页面缺失，查找过：${candidates.join(", ")}`);
}

function closeServer(server: Server): Promise<void> {
  return new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
}
