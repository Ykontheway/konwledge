import { request } from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import type { Plugin } from "vite";

const namespace = "/v1/management";
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const statuses = new Set(["queued", "running", "passed", "failed", "blocked", "superseded"]);
const maxResponseBytes = 8 * 1024 * 1024;

type RuntimeConfig = { hostname: string; port: number; apiKey: string };
type Environment = Record<string, string | undefined>;

// 仅在 configureServer 中调用；不 loadEnv，不放入 Vite config/define/browser env。
export function managementConfig(env: Environment): RuntimeConfig | undefined {
  const origin = env.KNOWLEDGE_MANAGEMENT_ORIGIN;
  const apiKey = env.KNOWLEDGE_API_KEY;
  if (!origin || !apiKey || !apiKey.trim() || !/^[\x21-\x7e]+$/.test(apiKey)) return;
  const match = /^http:\/\/(127\.0\.0\.1|localhost|\[::1\]):([1-9][0-9]{0,4})$/.exec(origin);
  if (!match || Number(match[2]) > 65535) return;
  return {
    hostname: match[1] === "[::1]" ? "::1" : "127.0.0.1",
    port: Number(match[2]),
    apiKey,
  };
}

function integer(value: string, min: number, max: number): boolean {
  return /^(0|[1-9][0-9]*)$/.test(value) && Number.isSafeInteger(Number(value)) &&
    Number(value) >= min && Number(value) <= max;
}

export function allowedManagementPath(raw: string): boolean {
  if (raw.length > 2048 || /[#\\\s]/.test(raw)) return false;
  const [path, query = "", extra] = raw.split("?");
  if (!path || extra !== undefined) return false;
  const parts = path.split("/");
  if (parts[0] !== "" || parts[1] !== "v1" || parts[2] !== "management") return false;
  const resource = parts[3];
  const id = parts[4];
  const suffix = parts[5];
  let allowed: string[];
  if (resource === "jobs" && parts.length === 4) {
    allowed = ["limit", "offset", "status", "dataset_id", "dataset_version", "parent_job_id"];
  } else if (id && uuid.test(id) && parts.length === 5) {
    if (resource === "documents" || resource === "lineages") allowed = ["limit", "offset"];
    else if (["jobs", "revisions", "units"].includes(resource ?? "")) allowed = [];
    else return false;
  } else if (id && uuid.test(id) && parts.length === 6) {
    if (resource === "documents" && suffix === "diff") allowed = ["limit", "offset", "old_document_id"];
    else if (resource === "units" && suffix === "evidence") allowed = [];
    else return false;
  } else return false;
  const params = new URLSearchParams(query);
  const seen = new Set<string>();
  for (const [key, value] of params) {
    if (!allowed.includes(key) || seen.has(key)) return false;
    seen.add(key);
    if (key === "limit" && !integer(value, 1, 100)) return false;
    if (key === "offset" && !integer(value, 0, 100_000)) return false;
    if (key === "dataset_version" && !integer(value, 1, Number.MAX_SAFE_INTEGER)) return false;
    if (key === "status" && !statuses.has(value)) return false;
    if (key.endsWith("_id") && !uuid.test(value)) return false;
  }
  return suffix !== "diff" || seen.has("old_document_id");
}

function safeError(res: ServerResponse, status: number, code: string): void {
  if (res.destroyed || res.writableEnded) return;
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store", Connection: "close" });
  res.end(JSON.stringify({ error: {
    message: "Management request unavailable or rejected.",
    type: status >= 500 ? "internal_error" : "invalid_request_error",
    param: null, code,
  } }));
}

export function sameOriginRequest(req: IncomingMessage, port: number): boolean {
  const host = req.headers.host;
  const allowedHosts = new Set([`127.0.0.1:${port}`, `localhost:${port}`, `[::1]:${port}`]);
  if (port === 80) for (const name of ["127.0.0.1", "localhost", "[::1]"]) allowedHosts.add(name);
  if (!host || !allowedHosts.has(host)) return false;
  const expectedOrigin = new URL(`http://${host}`).origin;
  // 不信任 forwarded/proxy headers，也不把 same-site 当成 same-origin。
  const fetchSite = req.headers["sec-fetch-site"];
  if (fetchSite && !["same-origin", "none"].includes(String(fetchSite))) return false;
  const origin = req.headers.origin;
  if (origin !== undefined && origin !== expectedOrigin) return false;
  const referer = req.headers.referer;
  if (referer !== undefined) {
    try {
      const parsed = new URL(referer);
      if (parsed.origin !== expectedOrigin || parsed.username || parsed.password) return false;
    } catch { return false; }
  }
  for (const name of ["host", "origin", "referer", "sec-fetch-site"]) {
    let count = 0;
    for (let i = 0; i < req.rawHeaders.length; i += 2) {
      if (req.rawHeaders[i]?.toLowerCase() === name) count++;
    }
    if (count > 1) return false;
  }
  return true;
}

export function managementMiddleware(config: RuntimeConfig | undefined, port: () => number) {
  return (req: IncomingMessage, res: ServerResponse, next: () => void): void => {
    const path = req.url ?? "";
    if (!path.startsWith(namespace)) { next(); return; }
    // 所有校验先完成，再创建 upstream request；不是 proxyReq 事后拦截。
    if (!sameOriginRequest(req, port())) { safeError(res, 403, "management_proxy_forbidden"); return; }
    if (req.method !== "GET") { safeError(res, 405, "management_proxy_method"); return; }
    if (!allowedManagementPath(path) || req.headers["transfer-encoding"] ||
        (req.headers["content-length"] !== undefined && req.headers["content-length"] !== "0")) {
      safeError(res, 400, "management_proxy_validation"); return;
    }
    if (!config) { safeError(res, 503, "management_proxy_unavailable"); return; }
    // 新建白名单 headers，而非复制用户 headers：cookie/auth/forwarded 均不会传递。
    const upstream = request({
      hostname: config.hostname, port: config.port, method: "GET", path,
      headers: { Authorization: `Bearer ${config.apiKey}`, Accept: "application/json" },
      agent: false,
    }, (response) => {
      const status = response.statusCode ?? 502;
      if (status !== 200 || !response.headers["content-type"]?.startsWith("application/json")) {
        response.resume();
        safeError(res, status >= 400 && status <= 599 ? status : 502, "management_upstream_unavailable");
        return;
      }
      const chunks: Buffer[] = [];
      let size = 0;
      response.on("data", (chunk: Buffer) => {
        size += chunk.length;
        if (size > maxResponseBytes) {
          safeError(res, 502, "management_upstream_unavailable");
          upstream.destroy();
        } else chunks.push(chunk);
      });
      response.on("error", () => safeError(res, 502, "management_upstream_unavailable"));
      response.on("end", () => {
        if (res.destroyed || res.writableEnded) return;
        const body = Buffer.concat(chunks).toString("utf8");
        try {
          // B1 JSON 是唯一可转发的成功响应；不透传 redirect/cookie/raw error。
          const parsed: unknown = JSON.parse(body);
          if (body.includes(config.apiKey) || JSON.stringify(parsed).includes(config.apiKey)) throw new Error();
          res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
          res.end(body);
        } catch { safeError(res, 502, "management_upstream_unavailable"); }
      });
    });
    upstream.on("error", () => safeError(res, 502, "management_upstream_unavailable"));
    upstream.setTimeout(5000, () => {
      safeError(res, 504, "management_upstream_unavailable");
      upstream.destroy();
    });
    res.on("close", () => upstream.destroy());
    upstream.end();
  };
}

export function managementProxy(): Plugin {
  return {
    name: "loopback-management-proxy",
    apply: "serve",
    configureServer(server) {
      const config = managementConfig(process.env);
      server.middlewares.use(managementMiddleware(config, () => {
        const address = server.httpServer?.address();
        return address && typeof address !== "string" ? (address as AddressInfo).port : 0;
      }));
    },
    // 故意没有 configurePreviewServer；静态 preview/build 不代理。
  };
}
