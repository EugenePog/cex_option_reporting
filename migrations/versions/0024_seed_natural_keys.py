"""Natural keys for the seeded core tables (build 11: seed files without ids)

The seed loader no longer matches rows on `id` (the CSVs carry no ids any more; the database
assigns them). It matches every row on a natural key, and names parents by their keys
(user email, account label, sub-account name, box key). This makes those keys unique:

  core
    * strategy: + key varchar(64) NOT NULL — a box's fixed name for the seed files. Set from the
      name when the box is created (Box builder: the name, made unique in the account) and never
      changed by a rename. Existing boxes get their current name; when two boxes of one account
      share a name (e.g. a deleted one and a new one), the extra ones get "<name>-<id>" (active
      boxes keep the plain name). Unique per sub-account among boxes that are not deleted
      (partial index uq_strategy_key_active) — a deleted box keeps its key, so a new box may
      reuse it.
    * cex_account: unique (cex_code, label)                       uq_cex_account_label
    * subaccount: unique (cex_account_id, subacct_name)           uq_subaccount_name
    * strategy_rule: unique (subaccount_id, strategy_id, match_json)  uq_strategy_rule_match
    (user.email, contract_size and notification_setting were unique already.)

If existing rows already break one of these keys the migration stops and lists them, so nothing is
changed silently — merge or delete the duplicates, then run it again.

Id sequences of the seeded tables are set to max(id), so database-assigned ids never collide with
the ids the old loader wrote.

Revision ID: 0024_seed_natural_keys
Revises: 0023_strategy_notifications
Create Date: 2026-10-22
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_seed_natural_keys"
down_revision: str | None = "0023_strategy_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNIQUE = [  # (table, columns, constraint name)
    ("cex_account", ("cex_code", "label"), "uq_cex_account_label"),
    ("subaccount", ("cex_account_id", "subacct_name"), "uq_subaccount_name"),
    ("strategy_rule", ("subaccount_id", "strategy_id", "match_json"), "uq_strategy_rule_match"),
]
SEEDED = ("user", "cex_account", "subaccount", "strategy", "strategy_rule", "contract_size",
          "notification_setting")


def _duplicates(conn, table: str, cols: tuple[str, ...]) -> list:
    c = ", ".join(cols)
    return conn.execute(sa.text(
        f'SELECT {c}, array_agg(id ORDER BY id) AS ids, count(*) AS n FROM core."{table}" '
        f"GROUP BY {c} HAVING count(*) > 1")).all()


def upgrade() -> None:
    conn = op.get_bind()

    # 1) stop before changing anything when existing rows clash on a new key
    problems = []
    for table, cols, _name in UNIQUE:
        for row in _duplicates(conn, table, cols):
            problems.append(f"core.{table}: ids {list(row.ids)} share ({', '.join(cols)}) = "
                            f"{tuple(row)[:len(cols)]}")
    if problems:
        raise RuntimeError(
            "0024_seed_natural_keys: these rows would break the new unique keys — merge or delete "
            "the duplicates, then run `make migrate` again:\n  " + "\n  ".join(problems))

    # 2) strategy.key — the current name; duplicates in an account get "-<id>" (active first)
    op.add_column("strategy", sa.Column("key", sa.String(64), nullable=True), schema="core")
    op.execute("UPDATE core.strategy SET key = name")
    op.execute("""
        WITH d AS (
            SELECT id, row_number() OVER (PARTITION BY subaccount_id, name
                                          ORDER BY (deleted_at IS NOT NULL), id) AS rn
            FROM core.strategy)
        UPDATE core.strategy s SET key = left(s.name, 50) || '-' || s.id
        FROM d WHERE d.id = s.id AND d.rn > 1
    """)
    op.alter_column("strategy", "key", nullable=False, schema="core")
    op.create_index("uq_strategy_key_active", "strategy", ["subaccount_id", "key"], unique=True,
                    schema="core", postgresql_where=sa.text("deleted_at IS NULL"))

    # 3) unique natural keys of the other seeded tables
    for table, cols, name in UNIQUE:
        op.create_unique_constraint(name, table, list(cols), schema="core")

    # 4) id sequences at max(id): the database assigns ids from now on
    for table in SEEDED:
        op.execute(
            f"SELECT setval(pg_get_serial_sequence('core.\"{table}\"', 'id'), "
            f'COALESCE((SELECT MAX(id) FROM core."{table}"), 1), '
            f'(SELECT MAX(id) FROM core."{table}") IS NOT NULL)')


def downgrade() -> None:
    for table, _cols, name in reversed(UNIQUE):
        op.drop_constraint(name, table, schema="core", type_="unique")
    op.drop_index("uq_strategy_key_active", table_name="strategy", schema="core")
    op.drop_column("strategy", "key", schema="core")
