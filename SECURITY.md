# Security Policy

Please do not open public issues containing:

- Telegram bot tokens;
- chat IDs;
- Claude session transcripts;
- private memory contents;
- `.env` files;
- private keys, cookies, OAuth secrets, or API keys.

Use placeholders in reports. Include only:

- tool names;
- decision reasons such as `no_active_grant` or `outbound_count_exceeded`;
- hashed IDs when needed;
- sanitized config snippets.

This project is a guardrail, not a guarantee. It is designed for pure chat scenarios and should not be treated as complete protection for autonomous deployment or CI/CD agents.
