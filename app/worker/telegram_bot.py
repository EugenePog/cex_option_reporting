"""Telegram bot client — taken over from the existing monitor's `app/telegram_bot.py`.

Unchanged API (`send_message`, `send_status_update`, `send_error_alert`); adapted to this repo:
  * the module logger instead of `from app import logger`;
  * `send_message` returns True / False so the alerts worker only marks a notification as sent when
    Telegram accepted it (a failed one is retried on the next check);
  * the user-supplied text in the two helpers is HTML-escaped.
"""
from __future__ import annotations

import html
import logging

from telegram import Bot
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token, chat_id, base_url: str | None = None):
        # base_url: Bot API endpoint override (tests use a local stub); None = api.telegram.org
        self.bot = Bot(token=token, **({"base_url": base_url} if base_url else {}))
        self.chat_id = chat_id

    async def send_message(self, message: str, parse_mode: str = 'HTML') -> bool:
        """Send a message to Telegram"""
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=parse_mode
            )
            logger.info("Telegram notification sent successfully")
            return True
        except TelegramError as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    async def send_status_update(self, status: str) -> bool:
        """Send status update message"""
        message = f"ℹ️ <b>Monitor Status</b>\n\n{html.escape(status)}"
        return await self.send_message(message)

    async def send_error_alert(self, error_message: str) -> bool:
        """Send error alert"""
        message = f"⚠️ <b>Error Alert</b>\n\n<code>{html.escape(error_message)}</code>"
        return await self.send_message(message)
