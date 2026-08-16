"""Core-layer ORM models (schema: core) — settings / dimension tables.

These are the manually-managed tables (users, accounts, subaccounts, strategies, rules) plus a
couple of system tables (audit_log, pipeline_watermark). Silver/gold rows are scoped and tagged
via these. Kept in their own module; imported by app.db.models so a single import registers all.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

CORE = "core"


class CoreUser(Base):
    __tablename__ = "user"
    __table_args__ = {"schema": CORE}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="client")  # 'client' | 'admin'
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CexAccount(Base):
    __tablename__ = "cex_account"
    __table_args__ = {"schema": CORE}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.user.id"))
    cex_code: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(64))  # matches bronze.account_label (e.g. 'OKX_K')
    # Credentials stay in env for dev; nullable here so seeds need not carry secrets.
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    passphrase_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    flag: Mapped[str] = mapped_column(String(4), default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Subaccount(Base):
    __tablename__ = "subaccount"
    __table_args__ = {"schema": CORE}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cex_account_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.cex_account.id"))
    cex_code: Mapped[str] = mapped_column(String(16))
    subacct_name: Mapped[str] = mapped_column(String(64), default="")  # matches bronze.subacct_name
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Strategy(Base):
    __tablename__ = "strategy"
    __table_args__ = {"schema": CORE}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"))
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StrategyRule(Base):
    __tablename__ = "strategy_rule"
    __table_args__ = {"schema": CORE}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"))
    match_json: Mapped[dict] = mapped_column(JSONB)  # e.g. {"inst_pattern": "BTC-USD-*"}
    strategy_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.strategy.id"))
    priority: Mapped[int] = mapped_column(Integer, default=100)


class Instrument(Base):
    """Conformed instrument dimension — populated by the silver pipeline, not seeded."""

    __tablename__ = "instrument"
    __table_args__ = (
        UniqueConstraint("cex_code", "inst_id", name="uq_instrument_cex_inst"),
        {"schema": CORE},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cex_code: Mapped[str] = mapped_column(String(16))
    inst_id: Mapped[str] = mapped_column(String(64))
    underlying: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opt_type: Mapped[str | None] = mapped_column(String(2), nullable=True)  # 'C' | 'P'
    strike: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    expiry: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    contract_ccy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tick_size: Mapped[float | None] = mapped_column(Numeric, nullable=True)


class AuditLog(Base):
    """Admin action log — app-written, not seeded."""

    __tablename__ = "audit_log"
    __table_args__ = {"schema": CORE}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("core.user.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PipelineWatermark(Base):
    """Incremental-pipeline bookkeeping — pipeline-written, not seeded."""

    __tablename__ = "pipeline_watermark"
    __table_args__ = {"schema": CORE}

    stage: Mapped[str] = mapped_column(String(16), primary_key=True)   # 'silver' | 'gold'
    cex_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_processed_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
