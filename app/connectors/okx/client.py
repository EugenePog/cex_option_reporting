"""Thin OKX API client.

Wraps the official `okx` SDK plus a couple of raw REST calls (opt-summary), mirroring the
patterns in the reference sample code. This class returns *raw* OKX dicts; mapping to normalized
rows happens in `mappers.py`. Keeping raw access isolated here makes the connector easy to test
(record OKX JSON, replay in tests) and easy to mock.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.connectors.base import Credentials

# NOTE: imported lazily inside methods so the package imports without the SDK during scaffolding.
# from okx import Account, MarketData, PublicData, Trade

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.okx.com"

# OKX error codes that mean "temporary, try again" — safe to retry.
# 50026 System error / 50013 System busy / 50011 rate limit / 50004 endpoint timeout /
# 50001 service temporarily unavailable / 50014 system busy.
_TRANSIENT_CODES = {"50026", "50013", "50011", "50004", "50001", "50014"}


class OkxError(RuntimeError):
    """Permanent OKX API error (bad params, auth, etc.) — do not retry."""


class OkxTransientError(RuntimeError):
    """Temporary OKX API error (system busy / rate limit) — safe to retry."""


# Retry only transient errors, with exponential backoff, then give up (reraise).
_retry_transient = retry(
    retry=retry_if_exception_type(OkxTransientError),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(4),
    reraise=True,
)


class OkxClient:
    def __init__(self, creds: Credentials) -> None:
        self._k = creds.api_key
        self._s = creds.api_secret
        self._p = creds.passphrase
        self._flag = creds.flag  # "0" live, "1" demo

    # -- SDK-backed calls (raw dicts) -------------------------------------- #
    @_retry_transient
    def get_account_balance(self) -> dict[str, Any]:
        import okx.Account as Account

        api = Account.AccountAPI(self._k, self._s, self._p, use_server_time=False, flag=self._flag)
        resp = api.get_account_balance()
        _raise_on_error(resp, "get_account_balance")
        return resp

    @_retry_transient
    def get_option_positions(self) -> dict[str, Any]:
        import okx.Account as Account

        api = Account.AccountAPI(self._k, self._s, self._p, use_server_time=False, flag=self._flag)
        resp = api.get_positions(instType="OPTION")
        _raise_on_error(resp, "get_positions")
        return resp

    @_retry_transient
    def _fills_page(self, inst_type: str, after: str | None, limit: int) -> dict[str, Any]:
        """One page of fills. Uses get_fills_history (deep, ~1yr) when available, else get_fills.

        `after` is a billId cursor: the API returns records *older* than it.
        """
        import okx.Trade as Trade

        api = Trade.TradeAPI(self._k, self._s, self._p, use_server_time=False, flag=self._flag)
        # get_fills_history reaches further back; fall back to get_fills (last ~3 days) if the
        # installed SDK version lacks it.
        method = getattr(api, "get_fills_history", None) or api.get_fills
        kwargs: dict[str, Any] = {"instType": inst_type, "limit": str(limit)}
        if after:
            kwargs["after"] = after
        resp = method(**kwargs)
        _raise_on_error(resp, "get_fills")
        return resp

    def get_fills_paginated(
        self, inst_type: str = "OPTION", since_ms: int = 0, page_limit: int = 100,
        max_pages: int = 200,
    ) -> list[dict[str, Any]]:
        """Page fills backwards in time until older than `since_ms` (or history is exhausted).

        `since_ms = 0` → walk the full available depth (used by backfill).
        Returns raw OKX fill dicts.
        """
        collected: list[dict[str, Any]] = []
        after: str | None = None
        for _ in range(max_pages):
            try:
                data = self._fills_page(inst_type, after, page_limit).get("data", [])
            except OkxTransientError as e:
                # Transient error persisted through retries — keep what we have (best-effort).
                logger.warning("fills pagination stopped early after transient error: %s", e)
                break
            if not data:
                break
            collected.extend(data)
            # Stop once this page's oldest record predates the window.
            oldest_ts = int(data[-1].get("ts") or data[-1].get("fillTime") or 0)
            if since_ms and oldest_ts and oldest_ts < since_ms:
                break
            cursor = data[-1].get("billId") or data[-1].get("tradeId")
            if not cursor or len(data) < page_limit:
                break  # last page
            after = str(cursor)
        return collected

    @_retry_transient
    def _positions_history_page(self, inst_type: str, after: str | None, limit: int) -> dict[str, Any]:
        import okx.Account as Account

        api = Account.AccountAPI(self._k, self._s, self._p, use_server_time=False, flag=self._flag)
        kwargs: dict[str, Any] = {"instType": inst_type, "limit": str(limit)}
        if after:
            kwargs["after"] = after
        resp = api.get_positions_history(**kwargs)
        _raise_on_error(resp, "get_positions_history")
        return resp

    def get_positions_history_paginated(
        self, inst_type: str = "OPTION", since_ms: int = 0, page_limit: int = 100,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        """Page closed positions (incl. expiry/delivery) backwards until older than `since_ms`.

        IMPORTANT: unlike fills-history (whose `after` is a billId), positions-history paginates
        by TIMESTAMP — `after` must be a millisecond `uTime`, not a posId. Passing a posId makes
        OKX 500 (it reads the cursor as a timestamp). `since_ms = 0` → full available depth.
        Returns raw OKX dicts; each carries `realizedPnl` — the source of expired-option PnL.
        """
        collected: list[dict[str, Any]] = []
        after: str | None = None
        for _ in range(max_pages):
            try:
                data = self._positions_history_page(inst_type, after, page_limit).get("data", [])
            except OkxTransientError as e:
                logger.warning(
                    "positions-history pagination stopped early after transient error "
                    "(kept %d records): %s", len(collected), e,
                )
                break
            if not data:
                break
            collected.extend(data)
            oldest_ts = int(data[-1].get("uTime") or data[-1].get("cTime") or 0)
            if since_ms and oldest_ts and oldest_ts < since_ms:
                break
            cursor = data[-1].get("uTime") or data[-1].get("cTime")   # timestamp cursor, not posId
            if not cursor or len(data) < page_limit or str(cursor) == after:
                break  # last page, or no forward progress
            after = str(cursor)
        return collected

    @_retry_transient
    def _bills_archive_page(self, inst_type: str, after: str | None, limit: int) -> dict[str, Any]:
        """One page of the account bills archive (~1yr). `after` is a billId cursor (like fills)."""
        import okx.Account as Account

        api = Account.AccountAPI(self._k, self._s, self._p, use_server_time=False, flag=self._flag)
        method = getattr(api, "get_account_bills_archive", None) or api.get_account_bills
        kwargs: dict[str, Any] = {"instType": inst_type, "limit": str(limit)}
        if after:
            kwargs["after"] = after
        resp = method(**kwargs)
        _raise_on_error(resp, "get_account_bills_archive")
        return resp

    def get_bills_paginated(
        self, inst_type: str = "OPTION", since_ms: int = 0, page_limit: int = 100,
        max_pages: int = 200,
    ) -> list[dict[str, Any]]:
        """Page the account ledger backwards until older than `since_ms` (bills use a billId cursor)."""
        collected: list[dict[str, Any]] = []
        after: str | None = None
        for _ in range(max_pages):
            try:
                data = self._bills_archive_page(inst_type, after, page_limit).get("data", [])
            except OkxTransientError as e:
                logger.warning("bills pagination stopped early after transient error "
                               "(kept %d records): %s", len(collected), e)
                break
            if not data:
                break
            collected.extend(data)
            oldest_ts = int(data[-1].get("ts") or 0)
            if since_ms and oldest_ts and oldest_ts < since_ms:
                break
            cursor = data[-1].get("billId")
            if not cursor or len(data) < page_limit or str(cursor) == after:
                break
            after = str(cursor)
        return collected

    # -- raw REST (not in SDK): option summary / IV+greeks ----------------- #
    @_retry_transient
    def get_opt_summary(self, uly: str, inst_id: str) -> dict[str, Any]:
        """GET /api/v5/public/opt-summary — IV, mark px, greeks. HMAC-signed (as in samples)."""
        import httpx

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        path = f"/api/v5/public/opt-summary?uly={uly}&instId={inst_id}"
        sign = base64.b64encode(
            hmac.new(self._s.encode(), (ts + "GET" + path).encode(), hashlib.sha256).digest()
        ).decode()
        headers = {
            "OK-ACCESS-KEY": self._k,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._p,
            "x-simulated-trading": self._flag,
            "Content-Type": "application/json",
        }
        resp = httpx.get(_BASE_URL + path, headers=headers, timeout=10.0).json()
        _raise_on_error(resp, "opt-summary")
        return resp


def _raise_on_error(resp: dict[str, Any], what: str) -> None:
    code = resp.get("code")
    if code != "0":
        msg = f"OKX {what} failed: {resp.get('msg')} (code {code})"
        if code in _TRANSIENT_CODES:
            raise OkxTransientError(msg)
        raise OkxError(msg)
