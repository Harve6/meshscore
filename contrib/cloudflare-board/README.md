# Cloudflare board host

A ~70 line Cloudflare Worker that hosts a bot's web page. The bot uploads its `index.html`, `state.json` and
`screen.png` straight to it after every round - no git, no GitHub Pages deploy delay, no repo history growing. Free
plan is enough (the files live in a SQLite-backed Durable Object, which the free plan includes).

```
bot  --PUT /api/files (Bearer TOKEN)-->  Worker + Durable Object  <--GET /, /state.json, /screen.png--  viewers
```

## Deploy

You need Node.js (for Cloudflare's `wrangler` tool) and a Cloudflare account.

```bat
cd contrib\cloudflare-board
npx wrangler login                     :: opens the browser: log in to Cloudflare and click Allow
npx wrangler deploy                    :: prints https://meshscore-board.<you>.workers.dev
npx wrangler secret put TOKEN          :: paste a long random string (the bot's upload password)
```

Then in `meshscore.toml`:

```toml
[bots.pokemon]
type = "pokemon"
channel = "#pokemon"
rom = "C:/path/to/Pokemon Red.gb"
site_api = "https://meshscore-board.<you>.workers.dev"
site_token = "<the same random string>"
site_url = "https://meshscore-board.<you>.workers.dev/"
```

`meshscore.toml` is git-ignored; keep the token there only. Anyone can read the page; only the token holder can write.

## Details

* `PUT /api/files` with `{"files": {"state.json": {"type": "application/json", "b64": "..."}}}`; names are
  `[A-Za-z0-9._-]{1,64}`, the body is capped at 1 MB.
* `GET /<name>` serves a file (`/` is `index.html`).
* The bot only sends files that changed, at most once every `publish_min_seconds` (default 4), and backs off when the
  Worker is unreachable. A failed upload never affects the game.
* One Worker hosts one board. Deploy a second copy under another `name` in `wrangler.toml` for another bot.
