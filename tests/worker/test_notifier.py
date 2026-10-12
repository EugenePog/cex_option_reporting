"""Telegram delivery (the monitor's TelegramNotifier + sync wrapper) against a local stub API."""
from __future__ import annotations

import pytest

from app.worker import notifier
from config.settings import get_settings
from tests.worker.stub_telegram import StubTelegram


@pytest.fixture
def telegram(monkeypatch):
    def configure(stub: StubTelegram | None, token="123:TEST", chat="4242"):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token if stub else "")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", chat if stub else "")
        monkeypatch.setenv("TELEGRAM_API_BASE_URL", stub.base_url if stub else "")
        get_settings.cache_clear()
    monkeypatch.setattr(notifier, "SEND_INTERVAL_S", 0)
    yield configure
    get_settings.cache_clear()


def test_send_many_delivers_html_in_order(telegram):
    with StubTelegram() as stub:
        telegram(stub)
        assert notifier.is_configured()
        assert notifier.send_many(["🔴 <b>one</b>", "🟡 <b>two</b>"]) == [True, True]
    assert [m["text"] for m in stub.sent] == ["🔴 <b>one</b>", "🟡 <b>two</b>"]
    assert {m["parse_mode"] for m in stub.sent} == {"HTML"}
    assert {str(m["chat_id"]) for m in stub.sent} == {"4242"}


def test_a_refused_message_is_reported_and_the_rest_still_go(telegram):
    with StubTelegram(fail_substring="bad") as stub:
        telegram(stub)
        assert notifier.send_many(["ok 1", "bad", "ok 2"]) == [True, False, True]
    assert [m["text"] for m in stub.sent] == ["ok 1", "ok 2"]


def test_wrong_token_fails_without_raising(telegram):
    with StubTelegram() as stub:
        telegram(stub, token="999:WRONG")
        assert notifier.send("hello") is False
    assert stub.sent == []


def test_not_configured_sends_nothing(telegram):
    telegram(None)
    assert not notifier.is_configured()
    assert notifier.send_many(["x"]) == [False]
    assert notifier.send_many([]) == []
