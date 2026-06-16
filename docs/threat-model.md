# Threat Model

The observed failure mode is an internal loop producing content that resembles a real user message. If the main model treats that content as user input, it may reply on Telegram or write long-term memory without a real external user action.

The guard avoids model-internal identity checks. It checks external facts:

- Was there a real inbound Telegram channel notification?
- Does it match the target chat and message?
- Is the grant still active?
- Has the outbound burst count, character budget, or burst window been exceeded?
- Is the memory write grounded in the real inbound message?

Default-deny behavior matters. If the hook cannot establish an active grant, it denies or routes to review.

Known non-goals:

- It does not solve long-task reporting without a future work-grant layer.
- It does not verify every possible Telegram plugin version automatically; hash mismatches fail closed and require re-audit.
- It does not make memory review decisions with another model in the realtime allow path.
