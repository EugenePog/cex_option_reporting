"""Telegram notifications — taken over from the existing monitor's `notifier.py`.

Wraps the async ``app.worker.telegram_bot.TelegramNotifier`` in a synchronous API so the (sync)
alerts worker can call it without having to be rewritten as async. Failures are logged but never
raised — a broken Telegram must not break the worker loop.

Adapted to this repo:
  * token / chat id come from config.settings (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` in .env)
    instead of the old `config` module;
  * HTML instead of Markdown (bold headers; box and account names are escaped);
  * `send_many` delivers a batch over one bot connection, about one message per second (Telegram's
    per-chat limit);
  * both functions return whether Telegram accepted the message(s).
"""
from __future__ import annotations

import asyncio
import logging

from app.worker.telegram_bot import TelegramNotifier
from config.settings import get_settings

log = logging.getLogger(__name__)

SEND_INTERVAL_S = 1.0     # pause between messages of one batch


def is_configured() -> bool:
    s = get_settings()
    return bool(s.telegram_bot_token and s.telegram_chat_id)


def send(message: str) -> bool:
    """Fire-and-forget Telegram message. Safe to call from sync code."""
    return send_many([message])[0]


def send_many(messages: list[str]) -> list[bool]:
    """Send messages in order; returns one flag per message (True = accepted by Telegram)."""
    if not messages:
        return []
    s = get_settings()
    if not s.telegram_bot_token or not s.telegram_chat_id:
        log.info("Telegram not configured (missing token or chat id); skipping")
        return [False] * len(messages)

    try:
        notifier = TelegramNotifier(
            s.telegram_bot_token,
            s.telegram_chat_id,
            base_url=s.telegram_api_base_url or None,
        )
        return asyncio.run(_deliver(notifier, messages))
    except Exception:
        log.exception("Failed to send Telegram notification")
        return [False] * len(messages)


async def _deliver(notifier: TelegramNotifier, messages: list[str]) -> list[bool]:
    sent: list[bool] = []
    async with notifier.bot:                  # opens and closes the bot's HTTP connection
        for i, message in enumerate(messages):
            if i:
                await asyncio.sleep(SEND_INTERVAL_S)
            sent.append(await notifier.send_message(message, parse_mode="HTML"))
    return sent
