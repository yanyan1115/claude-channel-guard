#!/usr/bin/env python3
"""Claude Code PreToolUse hook for MemoClover write tools."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .state import ChannelGuardState, MEMORY_WRITE_TOOLS, tool_suffix


def _hook_output(decision: str, reason: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload, ensure_ascii=False))


def _enqueue_memoclover_review(tool_name: str, args: dict[str, Any], reason: str, grant_id: str | None) -> bool:
    try:
        from memo_clover.memory_manager import enqueue_memory_write_review
    except Exception:
        return False
    result = enqueue_memory_write_review(
        tool_name=tool_name,
        tool_input=args,
        reason=reason,
        source_channel="telegram",
        source_grant_id=grant_id or "",
        reviewer="channel_guard",
        model="rule-based",
    )
    return bool(result.get("ok"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=os.environ.get("CHANNEL_GUARD_DIR", ""))
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        _hook_output("deny", "memory guard received invalid hook json")
        return 0

    tool_name = str(payload.get("tool_name") or "")
    suffix = tool_suffix(tool_name)
    if suffix not in MEMORY_WRITE_TOOLS:
        _hook_output("allow", "not a memory write tool")
        return 0

    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    state = ChannelGuardState(args.state_dir or None)
    decision = state.allow_memory_write(suffix, tool_input)
    state.log(
        "allow" if decision.allow else "deny",
        suffix,
        decision.reason,
        grant_id=decision.grant_id,
    )
    if decision.review:
        if not _enqueue_memoclover_review(suffix, tool_input, decision.reason, decision.grant_id):
            state.append_pending(suffix, tool_input, decision.reason, decision.grant_id)
    if decision.allow:
        _hook_output("allow", decision.reason)
    else:
        _hook_output("deny", decision.reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

