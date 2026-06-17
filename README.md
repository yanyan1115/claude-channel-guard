# Claude Channel Guard

External-source guardrails for Claude Code Telegram channels.

This project was built after observing a Claude Code loop failure mode where an internal loop could present text that looked like a real user message. The guard does not ask Claude to decide whether a message is real. Instead, it checks external channel facts before allowing sensitive actions.

## Scope

This release is for pure chat and IM-style workflows.

It is suitable for:

- Daily Telegram / IM chat with Claude Code.
- Guarding Telegram replies so one real inbound message opens one bounded reply grant.
- Guarding memory writes so forged loop content cannot silently enter long-term memory.

It is not yet suitable for:

- Long-running agentic work where Claude may work for many minutes and report later.
- CI/CD, deployment agents, autonomous production maintenance, or broad task automation.
- Any workflow that needs a work-grant model. That is intentionally left for a later version.

## What It Provides

- `telegram-outbound-guard`: an MCP stdio proxy that wraps the official Telegram channel server, records real inbound channel notifications, and blocks outbound reply/react/edit calls without an active grant.
- Append-only hashed inbound ledger: stores hashes and metadata, not raw chat text.
- SQLite grant state: tracks active grants, outbound count, character budget, burst window, and closed reasons.
- `memory-write-guard`: a Claude Code `PreToolUse` hook for memory write tools. It allows grounded low-risk remembers, routes uncertain writes to review/pending, and denies ungrounded writes.
- Recent memory intent lookback: if the user explicitly asked to remember something and a follow-up message arrives before Claude calls the memory tool, the guard can still use the recent real memory-intent inbound message as the memory source. Grounding checks still apply.
- Stop hook race fix: `close_consumed()` plus `--consumed-only`, so a late Stop event from a previous turn cannot close a just-created grant for a new Telegram message.

## Security Model

Claude is not trusted to prove that a user message is real. The guard trusts only:

- the Telegram channel server's real inbound notification stream;
- local private guard state;
- bounded grant accounting outside the model context.

If there is no real inbound grant, outbound Telegram actions and memory writes fail closed.

## Install

Clone and install into a Python environment visible to your Claude Code service:

```bash
git clone https://github.com/yanyan1115/claude-channel-guard.git
cd claude-channel-guard
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Create private state:

```bash
mkdir -p "$HOME/.claude/channel-guard"
chmod 700 "$HOME/.claude/channel-guard"
```

## Telegram Guarded Plugin

Do not edit the official plugin cache in place. Create a local guarded plugin that keeps the plugin name `telegram` but starts the Python proxy.

Copy `examples/telegram-guarded-plugin` to a local plugin directory, then edit `examples/telegram-guarded-plugin/.mcp.json` placeholders:

```bash
cp -a examples/telegram-guarded-plugin "$HOME/.claude/plugins/local/telegram-guarded"
```

The important command shape is:

```json
{
  "mcpServers": {
    "telegram": {
      "command": "/usr/bin/env",
      "args": [
        "PYTHONPATH=/path/to/claude-channel-guard",
        "CHANNEL_GUARD_DIR=/home/you/.claude/channel-guard",
        "/path/to/claude-channel-guard/.venv/bin/python",
        "-m",
        "channel_guard.telegram_mcp_proxy",
        "--upstream-dir",
        "/home/you/.claude/plugins/cache/claude-plugins-official/telegram/<version>",
        "--expected-server-sha256",
        "<sha256-of-server.ts>",
        "--state-dir",
        "/home/you/.claude/channel-guard"
      ]
    }
  }
}
```

Load the guarded plugin as a development channel:

```bash
claude --permission-mode auto \
  --plugin-dir "$HOME/.claude/plugins/local/telegram-guarded" \
  --dangerously-load-development-channels plugin:telegram@inline
```

Make sure the stock Telegram plugin is not also running. Telegram Bot API allows only one `getUpdates` consumer per bot token.

## Claude Code Hooks

Add the memory guard as a `PreToolUse` hook. The matcher must be `mcp__.*` or a narrower audited MCP server pattern such as `mcp__your_memory_server__.*`.

Do not use bare `mcp__`. Claude Code treats that as an exact matcher and it will match no MCP tools.

See `examples/claude-settings/settings.example.json`.

Add the Stop hook so consumed grants close when an assistant turn ends:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash /path/to/claude-channel-guard/hooks/channel-guard-close-grants.sh"
          }
        ]
      }
    ]
  }
}
```

The Stop hook uses `--consumed-only`; do not replace it with a blanket close unless you understand the race it prevents.

## Logs

Default state path:

```text
$HOME/.claude/channel-guard/
```

Files:

- `inbound-ledger.jsonl`: append-only hashed inbound facts.
- `guard_state.sqlite`: mutable grant state.
- `guard.log`: allow decisions.
- `blocked.log`: deny decisions.
- `memory-pending.log`: fallback pending queue when no memory review API is available.

Memory writes also use `CHANNEL_GUARD_MEMORY_INTENT_LOOKBACK` (default `600` seconds) to find a recent real inbound message with explicit memory intent. This is not a broad time-window allowlist: low-risk writes still need content grounding, and high-impact writes still go to review.

Logs should not contain token values, raw chat text, private keys, or chat IDs.

## Tests

```bash
python tests/test_channel_guard.py
```

## Acknowledgements

Built by Cora with help from Claude and GPT/Codex. The project exists because real users hit this failure mode in real Claude Code Telegram conversations, and sharing the guard may help others avoid the same loop.

## License

MIT
