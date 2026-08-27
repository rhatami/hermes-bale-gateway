# Bale Messenger Plugin for Hermes Agent

🇮🇷 [Bale (بله)](https://ble.ir) is an Iranian messaging platform with a Telegram-compatible Bot API. This plugin connects Hermes Agent directly to Bale — so you can chat with your AI assistant from Bale just like you do from Telegram.

## Features

- ✅ Text messaging (send & receive)
- ✅ Media support (images, documents, voice, video)
- ✅ Reply-to-message support
- ✅ User allowlist for security
- ✅ Cron/notification delivery to Bale
- ✅ Long message auto-splitting
- ✅ Zero external dependencies (uses `requests` already in Hermes)
- ✅ HTTP long-polling (no webhook/public URL needed)

## Quick Setup

### 1. Create a Bale Bot

1. Open Bale and find [@BotFather](https://ble.ir/BotFather)
2. Send `/newbot` and follow the instructions
3. Copy the bot token (format: `123456:abcdef...`)

### 2. Configure Hermes

```bash
# Set the bot token
hermes config set platforms.bale.token "YOUR_BOT_TOKEN"

# Enable the platform
hermes config set platforms.bale.enabled true

# Set your Bale user ID (get it from any Bale user-info bot)
hermes config set platforms.bale.home_channel.chat_id "YOUR_USER_ID"
hermes config set platforms.bale.home_channel.name "Your Name"
```

Or use environment variables:

```bash
export BALE_BOT_TOKEN="123456:abcdef..."
export BALE_ALLOWED_USERS="123456789"
export BALE_HOME_CHANNEL="123456789"
```

### 3. Restart Gateway

```bash
hermes gateway restart
```

### 4. Chat!

Open Bale, find your bot, and start chatting. 🎉

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BALE_BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `BALE_ALLOWED_USERS` | ❌ | Comma-separated user IDs (empty = allow all) |
| `BALE_ALLOW_ALL_USERS` | ❌ | Set to `true` to allow anyone (dev only) |
| `BALE_HOME_CHANNEL` | ❌ | Chat ID for cron/notification delivery |
| `BALE_HOME_CHANNEL_NAME` | ❌ | Display name for home channel |
| `BALE_POLL_INTERVAL` | ❌ | Polling interval in seconds (default: 2) |

## How It Works

Bale uses a Telegram-compatible Bot API at `https://tapi.bale.ai/bot`. This plugin:

1. **Polls** for new messages using `getUpdates` (HTTP long-polling)
2. **Routes** messages to the Hermes agent core
3. **Sends** responses back via `sendMessage`

No webhook or public URL is needed — it works from any machine with internet access.

## Plugin Structure

```
bale/
├── plugin.yaml     # Plugin metadata & env var declarations
├── __init__.py     # Exports register()
├── adapter.py      # BaleAdapter (main logic)
└── README.md       # This file
```
