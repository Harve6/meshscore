// A tiny file host for a MeshScore bot's web page, on Cloudflare Workers (free plan).
//
//   PUT /api/files   Authorization: Bearer <TOKEN>   {"files": {"state.json": {"type": "...", "b64": "..."}, ...}}
//   GET /<name>      the file (GET / is index.html)
//
// The bot PUTs its page, state.json and screen.png after every round; viewers GET them. Files live in one Durable
// Object (SQLite-backed, which the free plan includes), so there is no daily write quota to run into. The bot is the
// only writer; reads are public and edge-cached for a few seconds.

const NAME_RE = /^[A-Za-z0-9._-]{1,64}$/;
const MAX_BODY = 1_000_000;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const board = env.BOARD.get(env.BOARD.idFromName("main"));

    if (url.pathname.startsWith("/api/")) {
      const token = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
      const want = String(env.TOKEN || "").trim();   // a secret piped in from a shell can carry a trailing newline
      if (!want || token.trim() !== want) return new Response("unauthorized", { status: 401 });
      return board.fetch(request);
    }
    if (request.method !== "GET" && request.method !== "HEAD") return new Response("method not allowed", { status: 405 });
    return board.fetch(request);
  },
};

export class Board {
  constructor(state) {
    this.state = state;
  }

  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "PUT" && url.pathname === "/api/files") {
      const text = await request.text();
      if (text.length > MAX_BODY) return new Response("too large", { status: 413 });
      let body;
      try { body = JSON.parse(text); } catch { return new Response("bad json", { status: 400 }); }
      const files = (body && body.files) || {};
      const writes = {};
      for (const [name, f] of Object.entries(files)) {
        if (!NAME_RE.test(name) || !f || typeof f.b64 !== "string") continue;
        writes["f:" + name] = { type: String(f.type || "application/octet-stream"), b64: f.b64, at: Date.now() };
      }
      await this.state.storage.put(writes);
      return new Response(JSON.stringify({ ok: true, files: Object.keys(writes).length }), {
        headers: { "content-type": "application/json" },
      });
    }

    let name = decodeURIComponent(url.pathname.replace(/^\/+/, "")) || "index.html";
    if (!NAME_RE.test(name)) return new Response("not found", { status: 404 });
    const f = await this.state.storage.get("f:" + name);
    if (!f) {
      return new Response(name === "index.html" ? "Waiting for the bot's first publish..." : "not found", { status: 404 });
    }
    const bytes = Uint8Array.from(atob(f.b64), (c) => c.charCodeAt(0));
    return new Response(request.method === "HEAD" ? null : bytes, {
      headers: {
        "content-type": f.type,
        "cache-control": name === "index.html" ? "public, max-age=60" : "public, max-age=2",
        "access-control-allow-origin": "*",
      },
    });
  }
}
