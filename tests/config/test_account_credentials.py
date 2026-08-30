"""Per-account credential resolution: core.cex_account.label -> <LABEL>_* env vars."""
from __future__ import annotations

from config.settings import Settings


def test_credentials_for_label_reads_prefixed_env_vars(monkeypatch):
    monkeypatch.setenv("OKX_M_HEDGE_API_KEY", "key2")
    monkeypatch.setenv("OKX_M_HEDGE_API_SECRET", "secret2")
    monkeypatch.setenv("OKX_M_HEDGE_PASSPHRASE", "pass2")
    monkeypatch.setenv("OKX_M_HEDGE_FLAG", "1")

    creds = Settings().credentials_for_label("OKX_M_HEDGE")

    assert creds.api_key == "key2"
    assert creds.api_secret == "secret2"
    assert creds.passphrase == "pass2"
    assert creds.flag == "1"


def test_label_is_uppercased_and_flag_defaults(monkeypatch):
    monkeypatch.setenv("OKX_M_HEDGE_API_KEY", "k")
    monkeypatch.setenv("OKX_M_HEDGE_API_SECRET", "s")
    monkeypatch.setenv("OKX_M_HEDGE_PASSPHRASE", "p")
    monkeypatch.delenv("OKX_M_HEDGE_FLAG", raising=False)

    # lower-case label still resolves the upper-case env prefix
    creds = Settings().credentials_for_label("okx_m_hedge")

    assert creds.api_key == "k"
    assert creds.flag == "0"  # default when <LABEL>_FLAG is unset


def test_unconfigured_label_returns_empty(monkeypatch):
    # A label with no env block resolves to empty creds so the caller can skip it.
    for suffix in ("API_KEY", "API_SECRET", "PASSPHRASE", "FLAG"):
        monkeypatch.delenv(f"OKX_ABSENT_ACCT_{suffix}", raising=False)

    creds = Settings().credentials_for_label("OKX_ABSENT_ACCT")

    assert creds.api_key == ""
    assert creds.api_secret == ""
