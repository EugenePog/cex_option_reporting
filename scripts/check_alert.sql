-- Why did a Telegram box alert fire (again)? Read-only — it changes nothing.
--
--   docker exec -i cex_pg psql -U cex -d cex_option_reporting \
--     -v box=261127 -v account=OKX_K_BOX -v t1='2026-11-19 15:59' -v t2='2026-11-19 17:43' \
--     < scripts/check_alert.sql
--
-- box     = box (strategy) name, as in the message ("261127 · OKX_K_BOX · Box management half-prod")
-- account = core.cex_account.label or subaccount display_name (ILIKE pattern, '%' = any)
-- uly     = the box's underlying (default BTC-USD)
-- t1, t2  = the alert times (UTC, as printed in the message)
--
-- A price alert (a–d) fires once per crossing: sent → disarmed; re-armed only when
--   (A) the price is back on the other side of the level, or
--   (B) the box is not "alive" at a check (no leg open: every leg closed / expired / not in the
--       latest snapshot), or
--   (C) there was no state yet: a new box, or the box's legs moved to another box (new strategy_id).
-- After a re-arm it fires at the next check with the price over the level, but not sooner than
-- ALERTS_REARM_MINUTES (60) after the previous message. Sections 4–8 test A, B and C.
\set ON_ERROR_STOP on
\if :{?box}     \else \set box '261127'                    \endif
\if :{?account} \else \set account '%'                     \endif
\if :{?t1}      \else \set t1 '2026-11-19 15:59'           \endif
\if :{?t2}      \else \set t2 '2026-11-19 17:43'           \endif
\if :{?uly}     \else \set uly 'BTC-USD'                  \endif
\pset null '·'
SET TIME ZONE 'UTC';
SET client_min_messages = warning;

DROP TABLE IF EXISTS b;
CREATE TEMP TABLE b AS
SELECT st.id AS strategy_id, st.subaccount_id, st.name AS box, st.key, st.created_at,
       st.deleted_at, st.send_notifications, ca.label AS account_label, sa.subacct_name,
       sa.display_name
  FROM core.strategy st
  JOIN core.subaccount sa ON sa.id = st.subaccount_id
  JOIN core.cex_account ca ON ca.id = sa.cex_account_id
 WHERE st.name = :'box'
   AND (ca.label ILIKE :'account' OR coalesce(sa.display_name, '') ILIKE :'account');

\echo
\echo '== 1. The box (core.strategy) — created when? deleted? alerts on? (more than one row = same name in several accounts: narrow with -v account=…)'
SELECT * FROM b;

\echo
\echo '== 2. The box as graph ① and the worker see it now (gold.box_shape; updated_at = last gold run)'
SELECT bs.strategy_id, bs.underlying, bs.strike_lo AS bottom, bs.strike_hi AS top, bs.n_legs, bs.n_open_legs,
       bs.status, bs.started_at, bs.ends_at, bs.updated_at
  FROM gold.box_shape bs JOIN b USING (subaccount_id, strategy_id)
 WHERE bs.underlying = :'uly';

\echo
\echo '== 3. Its legs — which one sets the bottom, since when (opened / pinned into the box)'
SELECT g.strategy_id, g.inst_id, g.strike, g.side, g.size, g.status, g.pos_opened_at, g.closed_at,
       g.last_seen_at, g.expires_at, g.strategy_source, pin.created_at AS pinned_into_box_at,
       pin.created_by AS pinned_by
  FROM gold.position_leg g
  JOIN b USING (subaccount_id, strategy_id)
  LEFT JOIN silver.position_leg sl ON sl.id = g.position_leg_id
  LEFT JOIN core.strategy_link pin ON pin.id = sl.strategy_link_id
 WHERE g.underlying = :'uly'
 ORDER BY g.strategy_id, g.strike, g.pos_opened_at;

\echo
\echo '== 4. (C) Box builder changes touching this box — legs moved in / out, newest first'
SELECT l.created_at, l.action, l.inst_id, l.pos_id, l.pos_opened_at,
       l.prev_strategy_id AS from_box, l.strategy_id AS to_box, l.superseded_at, l.created_by,
       l.reason
  FROM core.strategy_link l JOIN b ON l.subaccount_id = b.subaccount_id
 WHERE l.strategy_id = b.strategy_id OR l.prev_strategy_id = b.strategy_id
 ORDER BY l.created_at DESC
 LIMIT 30;

\echo
\echo '== 5. Admin actions (Box builder, Settings) from 1 day before t1'
SELECT a.at, a.actor_user_id, a.action, a.target
  FROM core.audit_log a
 WHERE a.at >= :'t1'::timestamptz - interval '1 day'
 ORDER BY a.at DESC
 LIMIT 30;

\echo
\echo '== 6. Alert state of the box (core.notification_state) and the settings of its underlying'
SELECT ns.strategy_id, ns.underlying, ns.code, ns.armed, ns.last_sent_at, ns.last_value, ns.updated_at, ns.fired_for_ends_at
  FROM core.notification_state ns JOIN b USING (subaccount_id, strategy_id)
 ORDER BY ns.strategy_id, ns.underlying, ns.code;
SELECT ns.asset, ns.code, ns.threshold, ns.is_enabled, ns.is_critical, ns.updated_at
  FROM core.notification_setting ns
 WHERE ns.asset = :'uly'
 ORDER BY ns.code;

\echo
\echo '== 7. (A) Price vs the bottom level — the 1-minute index candles at both alerts, and whether the price was ever back above the level in between (a legitimate re-arm)'
WITH lvl AS (
    SELECT bs.strategy_id, bs.strike_lo * (1 + coalesce(st.threshold, 0) / 100.0) AS level
      FROM gold.box_shape bs JOIN b USING (subaccount_id, strategy_id)
      LEFT JOIN core.notification_setting st ON st.asset = bs.underlying AND st.code = 'bottom_strike'
     WHERE bs.underlying = :'uly'
), c AS (
    SELECT ts, low, high, close FROM silver.index_candle
     WHERE inst_id = :'uly' AND bar = '1m'
)
SELECT lvl.strategy_id, round(lvl.level, 1) AS bottom_level,
       (SELECT close FROM c WHERE ts = date_trunc('minute', :'t1'::timestamptz)) AS close_at_t1,
       (SELECT close FROM c WHERE ts = date_trunc('minute', :'t2'::timestamptz)) AS close_at_t2,
       (SELECT max(high) FROM c WHERE ts BETWEEN :'t1'::timestamptz AND :'t2'::timestamptz)
           AS max_between,
       (SELECT count(*) FROM c WHERE ts BETWEEN :'t1'::timestamptz AND :'t2'::timestamptz
                                 AND high > lvl.level) AS minutes_above_between,
       (SELECT max(ts) FROM c WHERE high > lvl.level) AS last_time_above_ever,
       (SELECT max(ts) FROM c) AS newest_candle
  FROM lvl;

\echo
\echo '== 8. (B) Snapshot runs around each alert with open legs of the box missing, a failed run or'
\echo '   no positions. box_legs_seen = 0 → none of its legs in the newest snapshot → "stale" → box'
\echo '   not alive at the next pipeline run → re-armed (fewer than expected but > 0 keeps it alive).'
\echo '   pos→bal s = seconds between the positions write and the balances write of one run: the'
\echo '   pipeline takes the newest BALANCE run as "now", so a pipeline run inside that gap sees all'
\echo '   legs as stale too — that leaves no trace here; compare with the pipeline log times.'
WITH runs AS (
    SELECT b.strategy_id, b.subaccount_id, b.subacct_name, r.ingest_id, r.started_at,
           r.finished_at, r.status
      FROM b JOIN bronze.ingest_run r ON r.account_label = b.account_label
     WHERE r.mode = 'snapshot'
       AND (r.started_at BETWEEN :'t1'::timestamptz - interval '75 min'
                             AND :'t1'::timestamptz + interval '2 min'
         OR r.started_at BETWEEN :'t2'::timestamptz - interval '75 min'
                             AND :'t2'::timestamptz + interval '2 min')
       -- the run is this sub-account's (ingest_run has no sub-account; its balance rows do)
       AND EXISTS (SELECT 1 FROM bronze.raw_balance x
                    WHERE x.ingest_id = r.ingest_id AND x.subacct_name = b.subacct_name)
), legs AS (
    SELECT g.subaccount_id, g.strategy_id, g.pos_id, g.pos_opened_at
      FROM gold.position_leg g JOIN b USING (subaccount_id, strategy_id)
     WHERE g.underlying = :'uly' AND g.status = 'open'
), chk AS (
    SELECT r.*,
           (SELECT count(*) FROM bronze.raw_position p
             WHERE p.ingest_id = r.ingest_id AND p.subacct_name = r.subacct_name) AS positions,
           (SELECT count(*) FROM legs l
             WHERE l.strategy_id = r.strategy_id AND l.pos_opened_at <= r.started_at) AS expected,
           (SELECT count(DISTINCT l.pos_id) FROM legs l
              JOIN bronze.raw_position p ON p.payload->>'posId' = l.pos_id
             WHERE l.strategy_id = r.strategy_id AND p.ingest_id = r.ingest_id
               AND p.subacct_name = r.subacct_name) AS box_legs_seen,
           round(extract(epoch FROM
                 (SELECT min(ingest_ts) FROM bronze.raw_balance x WHERE x.ingest_id = r.ingest_id)
               - (SELECT max(ingest_ts) FROM bronze.raw_position x WHERE x.ingest_id = r.ingest_id)
           )::numeric, 1) AS "pos→bal s"
      FROM runs r
)
SELECT strategy_id, started_at, finished_at, status, positions, box_legs_seen, expected,
       "pos→bal s"
  FROM chk
 WHERE box_legs_seen < expected OR status <> 'OK' OR positions = 0
 ORDER BY strategy_id, started_at;
\echo '   (no rows = every snapshot run in both 75-minute windows had all open legs of the box)'
\echo '   all runs in the windows, for scale:'
SELECT b.strategy_id, count(*) AS snapshot_runs,
       round(avg(extract(epoch FROM r.finished_at - r.started_at))::numeric, 1) AS avg_run_s
  FROM b JOIN bronze.ingest_run r ON r.account_label = b.account_label
 WHERE r.mode = 'snapshot'
   AND (r.started_at BETWEEN :'t1'::timestamptz - interval '75 min' AND :'t1'::timestamptz + interval '2 min'
     OR r.started_at BETWEEN :'t2'::timestamptz - interval '75 min' AND :'t2'::timestamptz + interval '2 min')
   AND EXISTS (SELECT 1 FROM bronze.raw_balance x
                WHERE x.ingest_id = r.ingest_id AND x.subacct_name = b.subacct_name)
 GROUP BY b.strategy_id;

\echo
\echo '== 9. Snapshot cadence — runs per hour for this account today (≈ 60 = every minute)'
SELECT date_trunc('hour', r.started_at) AS hour, count(*) AS runs,
       count(*) FILTER (WHERE r.status <> 'OK') AS not_ok
  FROM bronze.ingest_run r
 WHERE r.mode = 'snapshot' AND r.started_at >= date_trunc('day', :'t1'::timestamptz)
   AND r.account_label IN (SELECT account_label FROM b)
 GROUP BY 1 ORDER BY 1;
