/**
 * 本地联调服务：静态页 + Function 同进程，路径与线上网关一致（/functions/v1/app/*）。
 *
 * 只在 sites/dev 下使用，不参与打包；Deno 运行时本机不可用，这里用 Node 验证
 * handler 的业务语义，`Deno.serve` 入口本身仍需云端或 Deno 环境验证。
 */

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { dirname, extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { createHandler } from "../functions/handler.ts";

const ROOT = resolve(join(dirname(fileURLToPath(import.meta.url)), "..", "web"));
const PORT = Number(process.env.PORT || 8123);
const handler = createHandler((name) => process.env[name]);

const TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
  ".json": "application/json; charset=utf-8",
};

const server = createServer(async (request, response) => {
  const url = new URL(request.url || "/", `http://127.0.0.1:${PORT}`);
  if (url.pathname.startsWith("/functions/v1/app")) {
    const headers = new Headers();
    for (const [key, value] of Object.entries(request.headers)) {
      if (typeof value === "string") headers.append(key, value);
      else if (Array.isArray(value)) value.forEach((item) => headers.append(key, item));
    }
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(chunk as Buffer);
    const upstream = await handler(
      new Request(url, { method: request.method, headers, body: chunks.length ? Buffer.concat(chunks) : undefined }),
    );
    const body = Buffer.from(await upstream.arrayBuffer());
    response.writeHead(upstream.status, Object.fromEntries(upstream.headers));
    response.end(body);
    return;
  }
  const relative = normalize(url.pathname === "/" ? "index.html" : url.pathname.slice(1));
  if (relative.startsWith("..") || relative.includes("..")) {
    response.writeHead(403).end("forbidden");
    return;
  }
  try {
    const file = await readFile(join(ROOT, relative));
    response.writeHead(200, { "content-type": TYPES[extname(relative)] ?? "application/octet-stream" });
    response.end(file);
  } catch {
    response.writeHead(404, { "content-type": "text/plain; charset=utf-8" }).end("not found");
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`local preview: http://127.0.0.1:${PORT}`);
});
