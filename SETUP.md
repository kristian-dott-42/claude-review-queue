# Claude Review Queue

A one-click "read this later" bookmarklet that feeds a queue **Claude Desktop reads on demand** — no Discord, no bot, no server to host. Click the bookmarklet on pages through the day; in Claude Desktop, run the **Review my queue** prompt and Claude fetches each page and gives you a blunt *good / bad / ugly* triage so you only read what's worth it.

## How it works (30 seconds)

It's **one Python file** doing two jobs:

1. **An MCP server** that Claude Desktop launches. It exposes your queue to Claude as tools (`get_queue`, `clear_queue`, `remove_from_queue`) plus a ready-to-run **Review my queue** prompt.
2. **A tiny `localhost` capture endpoint** the bookmarklet hits (`http://127.0.0.1:8787/add`). It just appends the page to `~/.claude-review-queue/queue.jsonl`.

Because the capture endpoint rides inside the MCP server process, **it's live whenever Claude Desktop is open** — which for most of us is always. Nothing else to run. (If you want capture even while Claude is closed, see *Always-on* at the bottom.)

> There's no literal "push into Claude Desktop" — it has no inbound webhook. This is the native equivalent: Claude reads the queue through MCP, right where you already work.

## Requirements

- **Python 3.10+**
- **Claude Desktop**

## Install

```bash
cd claude-review-queue
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Then print your personalised config + bookmarklet (the token is generated once, locally):

```bash
.venv/bin/python server.py --print-config
```

That prints three things:

### 1. The Claude Desktop config

Merge the printed `review-queue` block into your `claude_desktop_config.json`:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

If the file is empty, paste the whole block. If it already has an `"mcpServers"` object, just add the `"review-queue"` entry inside it. It looks like:

```json
{
  "mcpServers": {
    "review-queue": {
      "command": "/abs/path/to/claude-review-queue/.venv/bin/python",
      "args": ["/abs/path/to/claude-review-queue/server.py"]
    }
  }
}
```

Then **fully quit and reopen Claude Desktop** (Cmd/Ctrl+Q — not just close the window).

### 2. The bookmarklet

Make a new bookmark in your browser and paste the printed `javascript:(function(){…})()` string as its **URL** (drag-to-bookmarks-bar also works). Name it something like **→ Claude queue**.

### 3. Use it

- On any page worth a later look, click the bookmarklet. A little "Queued for Claude ✅" tab flashes and auto-closes.
- In Claude Desktop, run the **Review my queue** prompt (the prompt menu — the `+` / "Add from MCP" area — lists it), or just type *"review my queue"*. Claude reads the queue, fetches each page, and ranks them GOOD / BAD / UGLY with a BS score, then offers to clear them.

> For the auto-fetch step, Claude Desktop needs to be able to read web pages — turn on **web search** in settings, or add a fetch MCP. Without it, Claude still lists the queue and you can paste pages in.

## Security notes

- The capture endpoint binds to **`127.0.0.1` only** — it is not reachable from your network.
- Every `/add` requires the **token** baked into your bookmarklet, so a random website can't silently inject items into your queue.
- Your token lives in `~/.claude-review-queue/config.json`. Re-run `--print-bookmarklet` any time to reprint it.

## Always-on capture (optional)

If you want the bookmarklet to work even when Claude Desktop is closed, run the same file as a standalone listener (e.g. as a macOS Login Item or `launchd` agent / Windows startup task):

```bash
.venv/bin/python server.py --serve-only
```

Use **either** the embedded mode **or** `--serve-only` — not both at once, or they'll fight over the port. The MCP server reads the same queue file regardless, so triage in Claude works either way.

## Files

- `server.py` — the whole thing (MCP server + capture listener + `--print-*` helpers)
- `requirements.txt` — one dependency, `mcp`
- Queue + config live in `~/.claude-review-queue/` (not in this folder)

## Handy commands

```bash
.venv/bin/python server.py --print-config       # config block + bookmarklet
.venv/bin/python server.py --print-bookmarklet  # just the bookmarklet
.venv/bin/python server.py --serve-only         # always-on capture daemon
```
