#!/usr/bin/env python3
"""MCP stdio proxy that guards the official Telegram channel server."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .state import ChannelGuardState, tool_suffix


OUTBOUND_TOOLS = {"reply", "react", "edit_message"}


def _send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _error_response(request_id: Any, tool_name: str, reason: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": f"telegram guard denied {tool_name}: {reason}",
                }
            ],
            "isError": True,
        },
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_upstream_hash(state: ChannelGuardState, upstream_server: Path, expected_hash: str) -> None:
    if not expected_hash:
        state.set_health("upstream_status", "ok")
        return
    actual = _sha256_file(upstream_server)
    if actual == expected_hash:
        state.set_health("upstream_status", "ok")
        state.set_health("upstream_hash", actual)
        return
    state.set_health("upstream_status", "upstream_hash_mismatch")
    state.set_health("upstream_hash", actual)
    state.log(
        "deny",
        "startup",
        "upstream_hash_mismatch",
        expected_hash=expected_hash[:16],
        actual_hash=actual[:16],
    )


def _resolve_bun() -> str:
    configured = os.environ.get("TELEGRAM_GUARD_BUN")
    if configured:
        return configured
    found = shutil.which("bun")
    if found:
        return found
    return "bun"


def _child_reader(child: subprocess.Popen[str], state: ChannelGuardState) -> None:
    assert child.stdout is not None
    for line in child.stdout:
        raw = line.rstrip("\n")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            sys.stdout.write(line)
            sys.stdout.flush()
            continue
        if msg.get("method") == "notifications/claude/channel":
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            content = str(params.get("content") or "")
            meta = params.get("meta") if isinstance(params.get("meta"), dict) else {}
            state.record_inbound(content, meta)
        _send(msg)


def _child_stderr_reader(child: subprocess.Popen[str]) -> None:
    assert child.stderr is not None
    for line in child.stderr:
        sys.stderr.write(line)
        sys.stderr.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-dir", default=os.environ.get("TELEGRAM_GUARD_UPSTREAM_DIR", ""))
    parser.add_argument("--expected-server-sha256", default=os.environ.get("TELEGRAM_GUARD_UPSTREAM_SERVER_SHA256", ""))
    parser.add_argument("--state-dir", default=os.environ.get("CHANNEL_GUARD_DIR", ""))
    args = parser.parse_args(argv)

    upstream_dir = Path(args.upstream_dir).expanduser()
    if not upstream_dir:
        print("telegram guard: --upstream-dir is required", file=sys.stderr)
        return 2
    upstream_server = upstream_dir / "server.ts"
    state = ChannelGuardState(args.state_dir or None)
    _check_upstream_hash(state, upstream_server, args.expected_server_sha256)
    bun = _resolve_bun()

    child = subprocess.Popen(
        [bun, "run", "--cwd", str(upstream_dir), "--shell=bun", "--silent", "start"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=os.environ.copy(),
    )
    threading.Thread(target=_child_reader, args=(child, state), daemon=True).start()
    threading.Thread(target=_child_stderr_reader, args=(child,), daemon=True).start()

    assert child.stdin is not None
    for line in sys.stdin:
        raw = line.rstrip("\n")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            child.stdin.write(line)
            child.stdin.flush()
            continue
        if msg.get("method") == "tools/call":
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            tool_name = tool_suffix(str(params.get("name") or ""))
            tool_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            if tool_name in OUTBOUND_TOOLS:
                decision = state.allow_telegram_outbound(tool_name, tool_args)
                state.log(
                    "allow" if decision.allow else "deny",
                    tool_name,
                    decision.reason,
                    grant_id=decision.grant_id,
                    chat_hash=state.hash_value(tool_args.get("chat_id")),
                )
                if not decision.allow:
                    _send(_error_response(msg.get("id"), tool_name, decision.reason))
                    continue
        child.stdin.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n")
        child.stdin.flush()

    try:
        child.terminate()
    except ProcessLookupError:
        pass
    return child.wait(timeout=5) if child.poll() is None else int(child.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
