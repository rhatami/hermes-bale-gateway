---
name: bale-gateway
description: "Connect Hermes to Bale Messenger as a gateway plugin."
category: integrations
---

# Bale Messenger Gateway for Hermes

## When to Use
User wants to chat with Hermes through Bale (بله), an Iranian messaging platform with a Telegram-compatible Bot API.

## Prerequisites
- Bale bot token from [@BotFather](https://ble.ir/BotFather)
- `requests` Python package (usually already in Hermes venv)

## Quick Setup

### 1. Plugin Location
The plugin lives at `~/.hermes/plugins/bale/`:
```
bale/
├── plugin.yaml     # Metadata & env var declarations
├── __init__.py     # Exports register()
├── adapter.py      # BaleAdapter (main logic)
└── README.md       # Setup guide
```

### 2. Configure
```bash
hermes config set platforms.bale.token "BOT_TOKEN"
hermes config set platforms.bale.enabled true
hermes config set platforms.bale.home_channel.chat_id "USER_ID"
hermes config set platforms.bale.home_channel.name "User Name"
```

### 3. Restart
```bash
hermes gateway restart  # MUST run from separate terminal, not inside Hermes
```

## Instant Acknowledgment (no typing indicator)
Bale doesn't support `sendChatAction` (typing indicator). The adapter can send an
instant "message received, processing..." ack right after each user message.

- Config via env `BALE_ACK_MESSAGE` or `platforms.bale.ack_message` in config.yaml
- Default text: "پیامت رسید 📩 دارم پردازشش می‌کنم، یه لحظه صبر کن…"
- Set to empty string to disable
- Skipped for: bot's own messages, empty updates, slash commands (`/model`, etc.)

## Ack Editing (single-message UX)
When the real answer is ready, the ack message is EDITED in place via
`editMessageText` instead of sending a new message — the user sees one message
that changes from "processing..." to the final answer.

- The ack message_id is stored per chat in `self._pending_acks`
- `send()` pops it and edits the ack with the first chunk; extra chunks (over
  ~4000 chars) are appended as new messages
- If the edit fails (old message, API error), it falls back to `sendMessage`
- Images (`sendPhoto`) are unaffected — still sent as new messages

## Bale API Details
- Base URL: `https://tapi.bale.ai/bot{token}/`
- Endpoints: identical to Telegram Bot API (getMe, getUpdates, sendMessage, sendPhoto, etc.)
- Auth: token in URL path, not header
- Max message: ~4000 chars
- Markdown: `*bold*` works, `**bold**` does NOT. `[link](url)` works.
- No `parse_mode` parameter needed for links
- `sendChatAction` may not work (undocumented)

## Key Design Decisions
- **Polling, not webhook**: No public URL needed. Works from any machine with internet.
- **Zero extra deps**: Only uses `requests` (already in Hermes).
- **Plugin pattern**: Inherits `BasePlatformAdapter`, registers via `ctx.register_platform()`.
- **Long-poll timeout**: 25s on getUpdates, 30s on API calls.

## Pitfalls
- Gateway restart from inside Hermes is blocked — use separate terminal or `systemctl --user restart hermes-gateway`
- Bale's markdown is NOT GitHub-flavored — prompt hint tells LLM to use plain text
- User IDs are numeric (like Telegram), not usernames
