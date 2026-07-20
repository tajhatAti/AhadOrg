# Ahad Co Telegram Bot (Service #3)

Always-on Telegram bot — long-polls Telegram in a background thread and runs a
tiny health server for Render.

## Deploy on Render

1. **New Web Service** → repo `tajhatAti/AhadOrg`, branch `main`,
   **Root Directory: `bot`**
2. Runtime: **Python**
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
3. Environment:
   - `TELEGRAM_BOT_TOKEN` = token from **@BotFather**
4. Deploy → service goes live and the bot starts answering in Telegram.

## Keep it awake 24/7 (free)

Render free instances sleep after ~15 min without HTTP traffic.
Fix: create a free **UptimeRobot** monitor that pings
`https://<your-bot-service>.onrender.com/health` every 5 minutes.
The poll thread then never sleeps and the bot answers instantly.

## Commands

| Command | Reply |
|---|---|
| `/start` | welcome message (bn) |
| `/help` | command list |
| `/echo <text>` | repeats text |
| `/time` | current UTC time |
| anything else | echoes it back |

## Roadmap (premium)

Later this service can serve **many users' bots** (per-user tokens stored in
the DB, gated behind a paid plan) — the poll loop is already isolated per
service instance, so a "spawn one thread per token" upgrade is
straightforward.