#!/usr/bin/env python3
"""Close active channel guard grants from Stop hooks or maintenance scripts."""

from __future__ import annotations

import argparse
import os

from .state import ChannelGuardState


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=os.environ.get("CHANNEL_GUARD_DIR", ""))
    parser.add_argument("--reason", default="stop_hook")
    parser.add_argument(
        "--consumed-only",
        action="store_true",
        help="Close only grants that already sent at least one outbound message.",
    )
    args = parser.parse_args()
    state = ChannelGuardState(args.state_dir or None)
    closed = state.close_consumed(args.reason) if args.consumed_only else state.close_all(args.reason)
    state.log("allow", "close_grants", args.reason, closed=closed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
