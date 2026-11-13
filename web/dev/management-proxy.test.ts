// @vitest-environment node
import { createServer as httpServer, request } from "node:http";
import type { IncomingHttpHeaders, Server, ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { randomBytes } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { build, createServer, preview, resolveConfig } from "vite";
import {
  allowedManagementPath, managementConfig, managementMiddleware, managementProxy,
} from "./management-proxy";

const id = "12345678-1234-1234-1234-123456789abc";
const servers: Server[] = [];
const apiKey = randomBytes(32).toString("hex");
const env = (origin = "http://localhost:8015", key = apiKey) => ({
  KNOWLEDGE_MANAGEMENT_ORIGIN: origin, KNOWLEDGE_API_KEY: key,
});
const port = (server: Server) => (server.address() as AddressInfo).port;
async function listen(server: Server): Promise<Server> {
  servers.push(server);
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  return server;
}
async function get(server: Server, path: string, headers: IncomingHttpHeaders = {}, method = "GET") {
  return new Promise<{ status: number; body: string; headers: IncomingHttpHeaders }>((done, reject) => {
    const req = request({ hostname: "127.0.0.1", port: port(server), path, method, headers, agent: false }, (res) => {
      let body = "";
      res.on("data", (chunk) => { body += chunk; });
      res.on("end", () => done({ status: res.statusCode!, body, headers: res.headers }));
    });
    req.on("error", reject);
    req.end();
  });
}
function frontend(target?: string) {
  const server = httpServer();
  // middleware 的 next 由 connect 提供；用包装器保持与 Vite 一样的调用方式。
  const middleware = managementMiddleware(target ? managementConfig(env(target)) : undefined, () => port(server));
  server.on("request", (req, res) => middleware(req, res, () => { res.end("static page"); }));
  return listen(server);
}

afterEach(async () => {
  vi.unstubAllEnvs();
  for (const server of servers.splice(0)) {
    server.closeAllConnections();
    await new Promise<void>((done, reject) => server.close((error) => error ? reject(error) : done()));
  }
});

describe("运行时配置 fail closed", () => {
  it.each([
    "http://example.com:8015", "https://127.0.0.1:8015", "http://127.0.0.2:8015",
    "http://127.1:8015", "http://2130706433:8015", "http://localhost.evil:8015",
    "http://user:password@localhost:8015", "http://localhost:8015/", "http://localhost:8015/a",
    "http://localhost:8015?key=a", "http://localhost:8015#key", "http://localhost",
    "http://localhost:0", "http://localhost:65536", "http://localhost:08015", " http://localhost:8015",
  ])("拒绝 target %s", (origin) => expect(managementConfig(env(origin))).toBeUndefined());
  it.each(["", " ", "\n", "secret\r\nInjected: x"])("拒绝空白/非法 secret", (key) => {
    expect(managementConfig(env(undefined, key))).toBeUndefined();
  });
  it("只读服务端变量并固定 localhost，不通过 DNS", () => {
    expect(managementConfig({ VITE_KNOWLEDGE_API_KEY: apiKey })).toBeUndefined();
    expect(managementConfig({ KNOWLEDGE_API_KEY: apiKey })).toBeUndefined();
    expect(managementConfig(env())).toEqual({ hostname: "127.0.0.1", port: 8015, apiKey });
    expect(managementConfig(env("http://[::1]:65535"))?.hostname).toBe("::1");
    expect(managementConfig(env("http://127.0.0.1:1"))?.port).toBe(1);
  });
});

describe("8 routes 和 query 白名单", () => {
  it.each([
    "/jobs?limit=100&offset=100000&status=queued&dataset_version=1", `/jobs/${id}`,
    `/revisions/${id}`, `/lineages/${id}?limit=20`, `/documents/${id}?offset=0`,
    `/documents/${id}/diff?old_document_id=${id}&limit=1`, `/units/${id}`, `/units/${id}/evidence`,
    `/jobs?dataset_id=${id}&parent_job_id=${id}`,
  ])("允许 %s", (path) => expect(allowedManagementPath(`/v1/management${path}`)).toBe(true));
  it.each([
    "/jobs/", "/jobs/no-uuid", `/jobs/${id}/delete`, "/sources", "/jobs?target=http://evil",
    "/jobs?api_key=secret", "/jobs?limit=101", "/jobs?limit=0", "/jobs?limit=1&limit=2",
    "/jobs?offset=100001", "/jobs?offset=-1", "/jobs?status=anything", "/jobs?dataset_id=bad",
    "/jobs?dataset_version=9007199254740992", `/units/${id}?limit=2`, `/documents/${id}/diff`,
    `/documents/${id}/diff?old_document_id=bad`, "/../health", "/jobs%2f../health",
    "/jobs#hash", "/jobs??limit=2", "/jobs\\anything", `/documents/${id}/../jobs`,
  ])("拒绝 %s", (path) => expect(allowedManagementPath(`/v1/management${path}`)).toBe(false));
});

describe("真实 HTTP：发送前校验、凭据隔离、固定错误", () => {
  let requests: IncomingHttpHeaders[];
  let upstream: Server;
  let web: Server;
  let respond: (res: ServerResponse) => void;
  beforeEach(async () => {
    requests = [];
    respond = (res) => {
      res.writeHead(200, { "Content-Type": "application/json", "Set-Cookie": "untrusted=value" });
      res.end('{"items":[]}');
    };
    upstream = await listen(httpServer((req, res) => { requests.push(req.headers); respond(res); }));
    web = await frontend(`http://localhost:${port(upstream)}`);
  });
  it("同源浏览器正常；客户端 auth/cookie/proxy/forwarded 均不透传", async () => {
    const origin = `http://127.0.0.1:${port(web)}`;
    const result = await get(web, "/v1/management/jobs", {
      origin, referer: `${origin}/`, "sec-fetch-site": "same-origin",
      authorization: "Bearer attacker", cookie: "session=attacker", "proxy-authorization": "attacker",
      forwarded: "host=evil", "x-forwarded-for": "evil", "x-forwarded-host": "evil",
      "x-forwarded-proto": "https", "x-api-key": "attacker", "x-real-ip": "evil",
    });
    expect(result.status).toBe(200);
    expect(requests).toHaveLength(1);
    expect(requests[0]?.authorization).toBe(`Bearer ${apiKey}`);
    for (const name of ["cookie", "proxy-authorization", "forwarded", "x-forwarded-for",
      "x-forwarded-host", "x-forwarded-proto", "x-api-key", "x-real-ip", "origin", "referer"]) {
      expect(requests[0]?.[name]).toBeUndefined();
    }
    expect(result.headers["set-cookie"]).toBeUndefined();
    expect(result.headers["access-control-allow-origin"]).toBeUndefined();
    expect(JSON.stringify(result)).not.toContain(apiKey);
  });
  it.each([
    { origin: "https://evil.example" }, { referer: "http://evil.example/" }, { origin: "null" },
    { "sec-fetch-site": "cross-site" }, { "sec-fetch-site": "same-site" },
    { host: "evil.example" }, { host: "127.0.0.1:1" }, { referer: "not a url" },
  ])("拒绝 foreign browser/Host %j 且无上游访问", async (headers) => {
    expect((await get(web, "/v1/management/jobs", headers)).status).toBe(403);
    expect(requests).toHaveLength(0);
  });
  it.each(["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])("拒绝方法 %s", async (method) => {
    expect((await get(web, "/v1/management/jobs", {}, method)).status).toBe(405);
    expect(requests).toHaveLength(0);
  });
  it("拒绝越界路径/查询/GET body，原静态页正常", async () => {
    for (const path of ["/v1/management/jobs?source=text", "/v1/management/jobs/no-uuid", "/v1/management/../health"]) {
      expect((await get(web, path)).status).toBe(400);
    }
    expect((await get(web, "/v1/management/jobs", { "content-length": "1" })).status).toBe(400);
    expect((await get(web, "/")).body).toBe("static page");
    expect(requests).toHaveLength(0);
  });
  it("未配置不挂上游 proxy，返回安全 JSON 503", async () => {
    const offline = await frontend();
    const result = await get(offline, "/v1/management/jobs");
    expect(result.status).toBe(503);
    expect(JSON.parse(result.body).error.code).toBe("management_proxy_unavailable");
    expect((await get(offline, "/")).status).toBe(200);
    expect(requests).toHaveLength(0);
  });
  it.each([302, 401, 404, 409, 410, 422, 500, 503])("上游 %s 不透传 key/target/rawstack/redirect", async (status) => {
    respond = (res) => {
      res.writeHead(status, { Location: "http://evil.example", "Content-Type": "application/json" });
      res.end(JSON.stringify({ stack: "raw stack", secret: apiKey }));
    };
    const result = await get(web, "/v1/management/jobs");
    expect(result.status).toBe(status === 302 ? 502 : status);
    expect(result.headers.location).toBeUndefined();
    expect(JSON.stringify(result)).not.toContain(apiKey);
    expect(result.body).not.toContain("raw stack");
    expect(result.body).not.toContain(String(port(upstream)));
  });
  it.each(["secret", "invalid", "html"])("成功响应也拦截 %s", async (mode) => {
    respond = (res) => {
      res.writeHead(200, { "Content-Type": mode === "html" ? "text/html" : "application/json" });
      res.end(mode === "secret" ? JSON.stringify({ key: apiKey }) : "not json");
    };
    const result = await get(web, "/v1/management/jobs");
    expect(result.status).toBe(502);
    expect(result.body).not.toContain(apiKey);
  });
  it("连接错误安全且不写日志", async () => {
    const offline = await frontend("http://127.0.0.1:1");
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    const result = await get(offline, "/v1/management/jobs");
    expect(result.status).toBe(502);
    expect(result.body).not.toContain(apiKey);
    expect(log).not.toHaveBeenCalled();
    log.mockRestore();
  });
});

it("Vite serve loopback/禁用 CORS；preview 不安装代理", async () => {
  vi.stubEnv("KNOWLEDGE_API_KEY", undefined);
  vi.stubEnv("KNOWLEDGE_MANAGEMENT_ORIGIN", undefined);
  const config = await resolveConfig({ configFile: resolve("vite.config.ts"), logLevel: "silent" }, "serve");
  expect(config.server.host).toBe("127.0.0.1");
  expect(config.server.cors).toBe(false);
  expect(config.server.allowedHosts).not.toBe(true);
  expect((config.server.allowedHosts as string[]).every((host) => ["localhost", "127.0.0.1"].includes(host))).toBe(true);
  expect(config.server.proxy).toBeUndefined();
  expect(managementProxy().configurePreviewServer).toBeUndefined();
  const vite = await createServer({ configFile: resolve("vite.config.ts"), logLevel: "silent", server: { port: 0 } });
  try {
    await vite.listen();
    const result = await get(vite.httpServer! as Server, "/v1/management/jobs");
    expect(result.status).toBe(503);
    expect(result.headers["access-control-allow-origin"]).toBeUndefined();
  } finally { await vite.close(); }
});

it("隔离最小 build secret-negative（不构建并发 UI 模块、不写 dist）", async () => {
  vi.stubEnv("KNOWLEDGE_API_KEY", apiKey);
  vi.stubEnv("KNOWLEDGE_MANAGEMENT_ORIGIN", "http://127.0.0.1:8015");
  const root = await mkdtemp(join(tmpdir(), "management-build-"));
  try {
    await writeFile(join(root, "index.html"), '<script type="module" src="/entry.js"></script>');
    await writeFile(join(root, "entry.js"), "console.log(import.meta.env, process.env)");
    const configFile = resolve("vite.config.ts");
    const config = await resolveConfig({ configFile, root, logLevel: "silent" }, "build");
    expect(JSON.stringify(config.env)).not.toContain(apiKey);
    expect(Object.keys(config.env).some((key) => key.startsWith("KNOWLEDGE_"))).toBe(false);
    const output = await build({ configFile, root, publicDir: false, logLevel: "silent", build: { write: false } });
    const text = JSON.stringify(output);
    expect(text).not.toContain(apiKey);
    expect(text).not.toContain("http://127.0.0.1:8015");
    expect(text).not.toContain("KNOWLEDGE_API_KEY");
  } finally { await rm(root, { recursive: true, force: true }); }
}, 15_000);

it("已配置凭据也不会让 preview 挂代理", async () => {
  let upstreamRequests = 0;
  const upstream = await listen(httpServer((_req, res) => { upstreamRequests++; res.end("upstream"); }));
  vi.stubEnv("KNOWLEDGE_API_KEY", apiKey);
  vi.stubEnv("KNOWLEDGE_MANAGEMENT_ORIGIN", `http://127.0.0.1:${port(upstream)}`);
  const root = await mkdtemp(join(tmpdir(), "management-preview-"));
  const server = await preview({
    configFile: resolve("vite.config.ts"), root, logLevel: "silent",
    build: { outDir: root }, preview: { port: 0 },
  });
  try {
    await writeFile(join(root, "index.html"), "static preview");
    const result = await get(server.httpServer as Server, "/v1/management/jobs");
    expect(upstreamRequests).toBe(0);
    expect(result.body).not.toContain(apiKey);
    expect(result.headers["access-control-allow-origin"]).toBeUndefined();
  } finally {
    await new Promise<void>((done, reject) => server.httpServer.close((error) => error ? reject(error) : done()));
    await rm(root, { recursive: true, force: true });
  }
});
