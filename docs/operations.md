# Operations

## Verify The Guarded Telegram Runtime

Use your own service name and paths:

```bash
systemctl show claude-telegram.service -p ActiveState -p SubState -p Result -p NRestarts --no-pager
systemd-cgls /system.slice/claude-telegram.service --no-pager
tmux -S /run/claude-telegram/tmux.sock ls
journalctl -u claude-telegram.service -n 100 --no-pager
```

Expected shape:

- one Claude Code Telegram service;
- one guarded local plugin;
- no second stock Telegram poller;
- no Telegram Bot API `409 Conflict`;
- guard logs in `CHANNEL_GUARD_DIR`.

## Hash Mismatch

If the official Telegram plugin `server.ts` changes, the proxy records `upstream_hash_mismatch` and fails closed. Re-audit the upstream plugin before updating `TELEGRAM_GUARD_UPSTREAM_SERVER_SHA256`.

## Tuning

Pure chat defaults:

- `CHANNEL_GUARD_MAX_OUTBOUND=8`
- `CHANNEL_GUARD_BURST_SECONDS=30`
- `CHANNEL_GUARD_MAX_CHARS=6000`
- `CHANNEL_GUARD_GRANT_START_TTL=600`
- `CHANNEL_GUARD_MEMORY_INTENT_LOOKBACK=600`

Increasing these values makes chat feel less constrained but increases the amount of output a single real inbound message can authorize.

The memory intent lookback is different from Telegram outbound burst authorization. It only helps memory-write guard find a recent real inbound message where the user explicitly asked to remember/update/delete something. It does not skip grounding checks and does not auto-apply high-impact updates.

Review-routed memory writes return `queued_for_review:<reason>` from the PreToolUse hook while still blocking the original write tool call. Treat that as "candidate captured for approval", not as an automatic memory update.
