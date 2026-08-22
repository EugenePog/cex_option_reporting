-- Trace expiring/closed options through bronze -> silver -> gold to find where they drop out.
-- Vars (override with psql -v d1=... ): d1/d2 = closed dates, exp1/exp2 = inst-id expiry YYMMDD.
\if :{?d1}
\else
  \set d1 '2026-08-21'
  \set d2 '2026-08-22'
  \set exp1 '260821'
  \set exp2 '260822'
\endif
\pset pager off
\echo '================ params ================'
\echo 'dates =' :'d1' :'d2' '  inst-id expiry codes =' :'exp1' :'exp2'

\echo '\n===== 0. ingest_run: did history/backfill run recently? ====='
SELECT mode, status, row_count, started_at, finished_at, left(coalesce(error_text,''),60) err
FROM bronze.ingest_run ORDER BY started_at DESC LIMIT 12;

\echo '\n===== 1. BRONZE raw_closed_position — overall coverage ====='
SELECT count(*) AS rows,
       to_timestamp(min((payload->>'uTime')::bigint)/1000) AT TIME ZONE 'UTC' AS earliest_close,
       to_timestamp(max((payload->>'uTime')::bigint)/1000) AT TIME ZONE 'UTC' AS latest_close
FROM bronze.raw_closed_position;

\echo '\n===== 1b. BRONZE closed positions whose INST-ID expiry is d1/d2 ====='
SELECT payload->>'instId' AS inst, payload->>'realizedPnl' AS realized, payload->>'ccy' AS ccy,
       to_timestamp((payload->>'uTime')::bigint/1000) AT TIME ZONE 'UTC' AS closed
FROM bronze.raw_closed_position
WHERE payload->>'instId' LIKE '%-' || :'exp1' || '-%'
   OR payload->>'instId' LIKE '%-' || :'exp2' || '-%'
ORDER BY closed;

\echo '\n===== 1c. BRONZE closed positions CLOSED ON d1/d2 (by uTime date) ====='
SELECT payload->>'instId' AS inst, payload->>'realizedPnl' AS realized,
       to_timestamp((payload->>'uTime')::bigint/1000) AT TIME ZONE 'UTC' AS closed
FROM bronze.raw_closed_position
WHERE (to_timestamp((payload->>'uTime')::bigint/1000) AT TIME ZONE 'UTC')::date IN (:'d1'::date, :'d2'::date)
ORDER BY closed;

\echo '\n===== 1d. BRONZE raw_position — were these expiries ever open in a snapshot? ====='
SELECT DISTINCT payload->>'instId' AS inst
FROM bronze.raw_position
WHERE payload->>'instId' LIKE '%-' || :'exp1' || '-%'
   OR payload->>'instId' LIKE '%-' || :'exp2' || '-%';

\echo '\n===== 2. SILVER closed_position for d1/d2 (by expiry or closed_at) ====='
SELECT inst_id, expiry, side, size, realized_pnl, ccy, closed_at, subaccount_id, strategy_id
FROM silver.closed_position
WHERE expiry IN (:'d1'::date, :'d2'::date) OR closed_at::date IN (:'d1'::date, :'d2'::date)
ORDER BY closed_at;

\echo '\n===== 2b. SILVER closed_position coverage + unresolved check ====='
SELECT count(*) AS rows, min(closed_at) AS earliest, max(closed_at) AS latest,
       count(*) FILTER (WHERE subaccount_id IS NULL) AS null_subaccount
FROM silver.closed_position;

\echo '\n===== 2c. Bronze-vs-silver count for d1/d2 (gap = pipeline/resolve issue) ====='
SELECT
 (SELECT count(*) FROM bronze.raw_closed_position
    WHERE (to_timestamp((payload->>'uTime')::bigint/1000) AT TIME ZONE 'UTC')::date IN (:'d1'::date,:'d2'::date)) AS bronze_rows,
 (SELECT count(*) FROM silver.closed_position
    WHERE closed_at::date IN (:'d1'::date,:'d2'::date)) AS silver_rows;

\echo '\n===== 3. GOLD asset_pnl_daily for d1/d2 ====='
SELECT subaccount_id, ccy, date, realized_pnl, unrealized_pnl, fees, net_pnl
FROM gold.asset_pnl_daily WHERE date IN (:'d1'::date, :'d2'::date) ORDER BY date, ccy;

\echo '\n===== 3b. GOLD asset_pnl_daily — last 16 rows (see where series stops) ====='
SELECT date, ccy, realized_pnl, net_pnl FROM gold.asset_pnl_daily ORDER BY date DESC LIMIT 16;

\echo '\n===== 3c. GOLD deal_ledger for d1/d2 ====='
SELECT inst_id, expiry, close_type, side, size, realized_pnl, closed_at
FROM gold.deal_ledger
WHERE expiry IN (:'d1'::date, :'d2'::date) OR closed_at::date IN (:'d1'::date, :'d2'::date)
ORDER BY closed_at;

\echo '\n================ how to read this ================'
\echo '1b/1c empty  -> not ingested: re-run  make backfill  (or history);'
\echo '               if still empty, OKX positions-history has no settlement yet for those days.'
\echo 'bronze>0 but silver=0 (2c) -> run  make pipeline-silver  (or subaccount not seeded/resolved).'
\echo 'silver>0 but gold=0 (3)    -> run  make pipeline-gold  (gold not rebuilt since ingest).'
