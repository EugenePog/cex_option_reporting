"""Core-layer ORM models (schema: core) — settings / dimension tables.

These are the manually-managed tables (users, accounts, subaccounts, strategies, rules) plus a
couple of system tables (audit_log, pipeline_watermark) and the app-written manual strategy links
(strategy_link, written by the admin Box builder — NOT seeded). Silver/gold rows are scoped and
tagged via these. Kept in their own module; imported by app.db.models so a single import registers
all.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
    """A strategy — shown as a *box* in the admin Box builder.

    Deleting a box is a soft delete (`deleted_at`, 0018): its legs are pinned to the account's
    `unassigned` box in changeset `deleted_changeset_id`, its strategy_rule rows stop applying, and
    it disappears from the GUI; the row stays so silver/gold/pin history keep valid FKs and Undo of
    that changeset can restore it. The `unassigned` box itself can't be renamed or deleted (the
    pipeline finds it by name).
    """

    __tablename__ = "strategy"
    __table_args__ = (
        # target of strategy_link's composite FK (strategy must belong to the leg's subaccount)
        UniqueConstraint("id", "subaccount_id", name="uq_strategy_id_subaccount"),
        {"schema": CORE},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"))
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # soft delete (0018) — NULL = active
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("core.user.id", name="fk_strategy_deleted_by_user"), nullable=True)
    deleted_changeset_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)


class StrategyRule(Base):
    __tablename__ = "strategy_rule"
    __table_args__ = {"schema": CORE}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"))
    match_json: Mapped[dict] = mapped_column(JSONB)  # e.g. {"inst_pattern": "BTC-USD-*"}
    strategy_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.strategy.id"))
    priority: Mapped[int] = mapped_column(Integer, default=100)


class StrategyLink(Base):
    """Manual strategy link ("pin") for one position leg — written by the admin Box builder.

    Append-only: every Apply / Reset / Undo inserts new rows and stamps `superseded_at` on the row
    it replaces, so the table is its own history. The CURRENT state of a leg is its row with
    superseded_at IS NULL (at most one — partial unique index). action='pin' forces strategy_id onto
    the leg and beats every strategy_rule; action='unpin' hands the leg back to the rules.

    Leg key = (cex_code, subaccount_id, pos_id = OKX posId, pos_opened_at = OKX cTime), the same key
    as silver.position_leg. The composite FK (strategy_id, subaccount_id) → core.strategy(id,
    subaccount_id) guarantees a leg can only be pinned to a strategy of its own subaccount.
    """

    __tablename__ = "strategy_link"
    __table_args__ = (
        ForeignKeyConstraint(["strategy_id", "subaccount_id"],
                             ["core.strategy.id", "core.strategy.subaccount_id"],
                             name="fk_strategy_link_strategy_same_subaccount"),
        CheckConstraint("action IN ('pin', 'unpin')", name="ck_strategy_link_action"),
        CheckConstraint("(action = 'pin') = (strategy_id IS NOT NULL)",
                        name="ck_strategy_link_pin_has_strategy"),
        Index("uq_strategy_link_current", "cex_code", "subaccount_id", "pos_id", "pos_opened_at",
              unique=True, postgresql_where=text("superseded_at IS NULL")),
        Index("ix_strategy_link_changeset", "changeset_id"),
        {"schema": CORE},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"))
    cex_code: Mapped[str] = mapped_column(String(16))
    pos_id: Mapped[str] = mapped_column(String(64))
    pos_opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    inst_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(8))
    strategy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prev_strategy_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("core.strategy.id"), nullable=True)
    changeset_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("core.user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
