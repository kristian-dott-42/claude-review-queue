#!/usr/bin/env python3
"""
Claude Review Queue — a Discord-free, Claude-native take on the bookmarklet queue.

ONE process does two jobs:

  1. MCP server (stdio)  -> Claude Desktop launches it; exposes the queue to Claude
                            as tools + a ready-to-run "review_queue" prompt.
  2. Capture listener    -> a tiny localhost HTTP endpoint the bookmarklet hits:
                              GET /add?token=...&u=URL&title=...   (window.open friendly)
                            It just appends to ~/.claude-review-queue/queue.jsonl.

So: click the bookmarklet on pages during the day -> in Claude Desktop, run the
"review_queue" prompt (or say "review my queue") -> Claude fetches + triages each
one (good / bad / ugly) and you clear them. No Discord, no bot, no hosting.

The listener only runs while Claude Desktop has the MCP server alive (i.e. while
Claude Desktop is open). If you want capture even when Claude is closed, run this
same file standalone as a login item:  python server.py --serve-only

Handy one-liners:
  python server.py --print-config      # prints the Claude Desktop JSON block + bookmarklet
  python server.py --print-bookmarklet # just the bookmarklet, token baked in
  python server.py --serve-only        # run ONLY the capture listener (no MCP) as a daemon

Requires: Python 3.10+, and `pip install mcp` (see requirements.txt).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Storage — everything lives under ~/.claude-review-queue/
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("RQ_DATA_DIR") or (Path.home() / ".claude-review-queue"))
QUEUE_FILE = DATA_DIR / "queue.jsonl"
CONFIG_FILE = DATA_DIR / "config.json"
DEFAULT_PORT = int(os.environ.get("RQ_PORT", "8787"))


def load_config() -> dict:
    """Load (or first-time create) the local config: a capture token + port.

    The token stops any random website from silently injecting items into your
    queue via a drive-by `fetch('http://127.0.0.1:8787/add?...')`. It is generated
    once and stored locally; the bookmarklet carries the same value.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            cfg = {}
    changed = False
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(18)
        changed = True
    if not cfg.get("port"):
        cfg["port"] = DEFAULT_PORT
        changed = True
    if changed:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    return cfg


CONFIG = load_config()
TOKEN = CONFIG["token"]
PORT = int(os.environ.get("RQ_PORT", CONFIG["port"]))


# ---------------------------------------------------------------------------
# Queue helpers (JSONL, de-duplicated by URL, order preserved)
# ---------------------------------------------------------------------------
def enqueue(url: str, title: str) -> None:
    record = {"url": url, "title": (title or "").strip(), "ts": int(time.time())}
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE_FILE.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def read_queue() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    items, seen = [], set()
    for line in QUEUE_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = obj.get("url")
        if url and url not in seen:
            seen.add(url)
            items.append(obj)
    return items


def write_queue(items: list[dict]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text("".join(json.dumps(it) + "\n" for it in items))


def log(msg: str) -> None:
    """All human-facing chatter goes to stderr — stdout is reserved for the MCP
    JSON-RPC stream, and a stray print() there would corrupt the protocol."""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Capture listener — the bookmarklet target. Bound to loopback only.
# ---------------------------------------------------------------------------
ADDED_PAGE = """<!doctype html><meta charset=utf-8>
<title>Queued</title>
<body style="font:15px -apple-system,system-ui,sans-serif;padding:22px;color:#111">
<div style="font-size:20px">Queued for Claude &#9989;</div>
<div style="margin-top:8px;color:#555;max-width:340px">{title}</div>
<script>setTimeout(function(){{window.close()}},1200)</script>
</body>"""


class CaptureHandler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Loopback-only + token-gated; CORS open so the auto-close page renders anywhere.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 (stdlib naming)
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        one = lambda k: (qs.get(k) or [""])[0]

        if parsed.path == "/health":
            return self._send(200, json.dumps({"ok": True, "queued": len(read_queue())}),
                              "application/json")

        if parsed.path == "/add":
            if TOKEN and one("token") != TOKEN:
                return self._send(401, "<h3>Unauthorised</h3>")
            url = one("u")
            if not url:
                return self._send(400, "<h3>Missing url</h3>")
            enqueue(url, one("title"))
            return self._send(200, ADDED_PAGE.format(title=escape(one("title") or url)))

        return self._send(404, "<h3>Not found</h3>")

    def log_message(self, *args, **kwargs):  # silence default stderr access logging
        pass


def serve_capture(port: int) -> ThreadingHTTPServer | None:
    """Start the loopback capture listener. Returns the server, or None if the
    port is busy (e.g. another instance is already capturing) — in which case the
    MCP side still works against the existing queue file, just without re-binding."""
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), CaptureHandler)
    except OSError as exc:
        log(f"[capture] could not bind 127.0.0.1:{port} ({exc}). "
            f"Queue reads still work; another instance may already be listening.")
        return None
    log(f"[capture] listening on http://127.0.0.1:{port}/add")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ---------------------------------------------------------------------------
# Triage prompt — the "good / bad / ugly" rubric, generalised from review.py
# ---------------------------------------------------------------------------
def build_review_prompt() -> str:
    items = read_queue()
    if not items:
        return ("Your review queue is empty — nothing flagged yet. Click the bookmarklet "
                "on a page to add it, then run this prompt again.")
    listing = "\n".join(
        f"{i}. {it.get('title') or '(untitled)'}\n   {it['url']}"
        for i, it in enumerate(items, 1)
    )
    return f"""You are a sharp, skeptical content-triage assistant. Below is a batch of \
pages I flagged with a bookmarklet to "check before I commit to reading."

For each item: fetch the page (use your web/browse tools), then give a fast, honest verdict \
so I can decide what's actually worth my time. Rank them best -> worst and group under bold \
headings: **GOOD** (worth reading in full), **BAD** (one useful nugget, heavy caveats), \
**UGLY** (skip — clickbait / paywalled fluff / vendor pitch / AI filler). Omit a heading if \
nothing belongs in it.

For each item output exactly this shape:
**<n>. <short title>** — VERDICT [READ | SKIM | SKIP] · BS <1-5>/5
<one or two tight lines: what it really is, the single useful thing if any, and red flags — \
paywall, invented numbers, affiliate/vendor pivot, unverified claims, AI filler>
🔗 <url>

Rules: BS score 1 = solid/credible, 5 = pure hype. If a page can't be fetched (paywall or \
bot-block), say so and judge cautiously from the title/source — do NOT invent content you \
couldn't see. Be blunt and concise. No preamble, output only the triage.

When you're done, tell me you can clear the reviewed items from the queue with the \
`clear_queue` tool (or `remove_from_queue` for just some of them).

Current queue ({len(items)} item{'s' if len(items) != 1 else ''}):
{listing}
"""


# ---------------------------------------------------------------------------
# --print-* helpers (handoff convenience)
# ---------------------------------------------------------------------------
def bookmarklet() -> str:
    ep = f"http://127.0.0.1:{PORT}/add"
    return (
        "javascript:(function(){"
        f'var EP="{ep}";var TK="{TOKEN}";'
        "var u=EP+'?token='+encodeURIComponent(TK)+'&u='+encodeURIComponent(location.href)"
        "+'&title='+encodeURIComponent(document.title);"
        "window.open(u,'rq','width=420,height=200');})();"
    )


def claude_desktop_config_block() -> str:
    py = os.path.abspath(sys.executable)
    script = os.path.abspath(__file__)
    block = {
        "mcpServers": {
            "review-queue": {
                "command": py,
                "args": [script],
            }
        }
    }
    return json.dumps(block, indent=2)


def print_config() -> None:
    print("\n=== 1. Claude Desktop config ===")
    print("Merge this into your claude_desktop_config.json")
    print("  macOS:   ~/Library/Application Support/Claude/claude_desktop_config.json")
    print("  Windows: %APPDATA%\\Claude\\claude_desktop_config.json")
    print("(if the file already has \"mcpServers\", add the \"review-queue\" entry inside it)\n")
    print(claude_desktop_config_block())
    print("\n=== 2. Bookmarklet (drag to bookmarks bar, or save as a bookmark URL) ===\n")
    print(bookmarklet())
    print("\n=== 3. Then ===")
    print("Restart Claude Desktop. Click the bookmarklet on any page to queue it.")
    print('In Claude Desktop, run the "review_queue" prompt (or say "review my queue").\n')


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
def run_mcp() -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        log("ERROR: the `mcp` package isn't installed. Run:  pip install mcp")
        sys.exit(1)

    # Bring up the capture listener alongside the MCP server (same process).
    serve_capture(PORT)

    mcp = FastMCP("review-queue")

    @mcp.tool()
    def get_queue() -> str:
        """Return everything currently in the review queue (URL + title + when added).
        Use this to see what the user has flagged for triage."""
        items = read_queue()
        if not items:
            return "The review queue is empty."
        lines = []
        for i, it in enumerate(items, 1):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(it.get("ts", 0)))
            lines.append(f"{i}. {it.get('title') or '(untitled)'}\n   {it['url']}   [added {when}]")
        return f"{len(items)} item(s) in the queue:\n\n" + "\n".join(lines)

    @mcp.tool()
    def clear_queue() -> str:
        """Empty the entire review queue. Call this after the user has reviewed everything."""
        n = len(read_queue())
        write_queue([])
        return f"Cleared {n} item(s). The queue is now empty."

    @mcp.tool()
    def remove_from_queue(urls: list[str]) -> str:
        """Remove specific items from the queue by their exact URL(s). Use this when only
        some items have been dealt with and the rest should stay queued."""
        drop = set(urls or [])
        if not drop:
            return "No URLs given; nothing removed."
        before = read_queue()
        kept = [it for it in before if it.get("url") not in drop]
        write_queue(kept)
        return f"Removed {len(before) - len(kept)} item(s); {len(kept)} still queued."

    @mcp.prompt(title="Review my queue")
    def review_queue() -> str:
        """Fetch and triage everything in the review queue (good / bad / ugly)."""
        return build_review_prompt()

    mcp.run()  # stdio transport; blocks.


def run_serve_only() -> None:
    """Standalone always-on capture, for running as a login item when you want the
    bookmarklet to work even while Claude Desktop is closed."""
    httpd = serve_capture(PORT)
    if httpd is None:
        sys.exit(1)
    log("[serve-only] capture daemon running. Ctrl-C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("[serve-only] stopping.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Claude-native review queue (MCP + bookmarklet capture).")
    ap.add_argument("--serve-only", action="store_true",
                    help="run ONLY the capture listener (no MCP), e.g. as an always-on login item")
    ap.add_argument("--print-config", action="store_true",
                    help="print the Claude Desktop config block + bookmarklet, then exit")
    ap.add_argument("--print-bookmarklet", action="store_true",
                    help="print just the bookmarklet (token baked in), then exit")
    args = ap.parse_args()

    if args.print_bookmarklet:
        print(bookmarklet())
        return
    if args.print_config:
        print_config()
        return
    if args.serve_only:
        run_serve_only()
        return
    run_mcp()


if __name__ == "__main__":
    main()
