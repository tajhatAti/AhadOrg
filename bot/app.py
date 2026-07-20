"""
Ahad Co Telegram Bot — always-on bot service (Service #3).

What this is
------------
A standalone Telegram bot that STAYS ALIVE (unlike the one-shot playground
runs in the main site). It works by "long polling": a background thread keeps
asking Telegram for new messages and replies to them. A tiny FastAPI server
runs alongside purely so Render's health checks have something to ping.

Env vars
--------
    TELEGRAM_BOT_TOKEN   (required) — the token @BotFather gave you.

Commands the bot understands
----------------------------
    /start   — welcome message
    /help    — list of commands
    /echo x  — repeats x back to the user
    /time    — current UTC time
    anything else — echoed back

Keep-alive note
---------------
Render free plan sleeps idle services after ~15 min. Register the service URL
(e.g. https://your-bot.onrender.com/health) in a free UptimeRobot monitor
(5-min interval) to keep the bot awake 24/7.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ahad-bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""
POLL_TIMEOUT_S = 40          # long-poll wait per request (Telegram max ~50)
ERROR_BACKOFF_S = 5          # wait before retrying after a network hiccup

app = FastAPI(title="Ahad Co Telegram Bot")

# Session reuses TCP connections — much faster than fresh requests calls.
_http = requests.Session()


# ---------------------------------------------------------------------------
# Reply logic (pure function — easy to unit test, no network involved)
# ---------------------------------------------------------------------------
def build_reply(text: str, first_name: str) -> str:
    """Given the raw message text, decide what the bot answers."""
    t = (text or "").strip()
    low = t.lower()

    if low.startswith("/start"):
        return (
            f"👋 Hello {first_name}! Ahad Co Bot live achhe!\n\n"
            "Ami ekhon basic — kintu 24/7 thaki. Commands:\n"
            "  /help  — sob command dekhaibo\n"
            "  /echo tomar msg — ami repeat korbo\n"
            "  /time  — somoy dekhaibo\n"
            "Ba ja iccha likhun — echo kore felbo 😄"
        )

    if low.startswith("/help"):
        return (
            "🤖 Ahad Co Bot commands:\n\n"
            "/start — intro\n"
            "/help  — ei message\n"
            "/echo <text> — text repeat kore\n"
            "/time  — UTC somoy\n\n"
            "Onno kichu likhle echo korbo."
        )

    if low.startswith("/echo"):
        payload = t[5:].strip()
        return f"📣 {payload}" if payload else "📣 /echo er pore kichu likhun!"

    if low.startswith("/time"):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"🕐 Ekhon: {now}"

    # Default: echo back whatever they wrote.
    return f"🪞 Apni likhchen: {t}"


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------
def _tg(method: str, **params) -> dict:
    """Call a Telegram Bot API method; returns the parsed JSON (or {})."""
    if not TG_API:
        return {}
    try:
        r = _http.get(f"{TG_API}/{method}", params=params, timeout=POLL_TIMEOUT_S + 25)
        return r.json()
    except Exception as exc:  # network hiccup — caller retries on next loop
        logger.warning("Telegram %s failed: %s", method, exc)
        return {}


def _send(chat_id: int, text: str) -> None:
    _tg("sendMessage", chat_id=chat_id, text=text)


# ---------------------------------------------------------------------------
# Long-polling loop (runs in a background thread forever)
# ---------------------------------------------------------------------------
def _poll_loop() -> None:
    logger.info("🤖 Bot polling started")
    offset = 0
    while True:
        updates = _tg("getUpdates", offset=offset, timeout=POLL_TIMEOUT_S)

        # Telegram-side rejection (bad token / another instance polling).
        if updates and not updates.get("ok"):
            desc = updates.get("description", "?")
            logger.error("getUpdates error: %s — retrying in %ss", desc, ERROR_BACKOFF_S * 2)
            time.sleep(ERROR_BACKOFF_S * 2)
            continue

        for upd in updates.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            text = msg.get("text")
            if not chat_id or not text:
                continue  # ignore photos/stickers/etc for now
            first_name = (msg.get("from") or {}).get("first_name") or "bondhu"
            reply = build_reply(text, first_name)
            _send(chat_id, reply)
            logger.info("↩️  answered %s in chat %s", text[:30], chat_id)

        time.sleep(0.2)  # be polite between empty polls


@app.on_event("startup")
def _start_polling() -> None:
    if not BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set — bot asleep. Web server still up.")
        return
    thread = threading.Thread(target=_poll_loop, name="telegram-poller", daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Web endpoints (only for Render health checks + curious visitors)
# ---------------------------------------------------------------------------
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {
        "service": "ahad-telegram-bot",
        "status": "alive" if BOT_TOKEN else "no token set",
        "note": "Talk to the bot in Telegram. This page is just a health beacon.",
    }


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok", "bot_configured": bool(BOT_TOKEN)}
