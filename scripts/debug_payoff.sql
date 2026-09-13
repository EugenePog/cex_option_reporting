-- Diagnose report ② (Payoff): which expiries exist, and whether a settlement (underlying) price
-- is available for each. Vars: uly (underlying), exp (a specific expiry to inspect).
\if :{?uly}
\else
  \set uly 'BTC-USD'
\endif
\pset pager off

\echo '================ params ================'
\echo 'underlying =' :'uly'

\echo '\n===== 0. gold row counts relevant to report 2 ====='
SELECT 'deal_ledger' t, count(*) FROM gold.deal_ledger
UNION ALL SELECT 'underlying_price', count(*) FROM gold.underlying_price
UNION ALL SELECT 'position_current', count(*) FROM gold.position_current;

\echo '\n===== 1. THE SMOKING GUN: snapshot window vs deal history window ====='
\echo '(underlying_price is built from silver.position_snapshot; if snapshots start AFTER most'
\echo ' expiries, those expiries get no settlement price.)'
SELECT 'silver.position_snapshot' src, min(captured_at) earliest, max(captured_at) latest
FROM silver.position_snapshot
UNION ALL
SELECT 'gold.underlying_price', min(captured_at), max(captured_at) FROM gold.underlying_price
UNION ALL
SELECT 'gold.deal_ledger.closed_at', min(closed_at), max(closed_at) FROM gold.deal_ledger;

\echo '\n===== 2. distinct expiries in deal_ledger (what report 2 CAN show for expired) ====='
SELECT expiry, count(*) legs, count(DISTINCT inst_id) insts,
       round(sum(realized_pnl)::numeric,6) realized, min(closed_at) first_close, max(closed_at) last_close
FROM gold.deal_ledger WHERE expiry IS NOT NULL GROUP BY expiry ORDER BY expiry;

\echo '\n===== 3. open positions (what report 2 shows by default) ====='
SELECT inst_id, underlying, expiry, side, size, idx_px, captured_at
FROM gold.position_current ORDER BY expiry, inst_id;

\echo '\n===== 4. per-expiry settlement availability (px on/before expiry?) ====='
\echo '(px_rows=0 => no expiration price for that expiry => marker falls back to avg strike)'
WITH exps AS (SELECT DISTINCT underlying, expiry FROM gold.deal_ledger WHERE expiry IS NOT NULL)
SELECT e.underlying, e.expiry,
  (SELECT count(*) FROM gold.underlying_price up
     WHERE up.underlying = e.underlying
       AND up.captured_at <= (e.expiry + INTERVAL '1 day')) AS px_rows_on_or_before,
  (SELECT max(up.captured_at) FROM gold.underlying_price up
     WHERE up.underlying = e.underlying
       AND up.captured_at <= (e.expiry + INTERVAL '1 day')) AS latest_px_at
FROM exps e ORDER BY e.expiry;

\echo '\n===== 5. underlying_price coverage by underlying ====='
SELECT underlying, count(*) rows, min(captured_at) earliest, max(captured_at) latest,
       min(idx_px) min_px, max(idx_px) max_px
FROM gold.underlying_price GROUP BY underlying ORDER BY underlying;

\echo '\n===== 6. settlement lookup for the LATEST expired expiry (:uly) ====='
WITH e AS (SELECT max(expiry) exp FROM gold.deal_ledger WHERE underlying = :'uly')
SELECT (SELECT exp FROM e) AS expiry,
  (SELECT up.idx_px FROM gold.underlying_price up, e
     WHERE up.underlying = :'uly' AND up.captured_at <= (e.exp + INTERVAL '1 day')
     ORDER BY up.captured_at DESC LIMIT 1) AS settlement_price,
  (SELECT up.captured_at FROM gold.underlying_price up, e
     WHERE up.underlying = :'uly' AND up.captured_at <= (e.exp + INTERVAL '1 day')
     ORDER BY up.captured_at DESC LIMIT 1) AS settlement_at;

\echo '\n===== 7. is the REAL settlement price already in bronze bills (delivery, type 3)? ====='
\echo '(if payload has a px/settlement field, we can source the true expiration price without a new API call)'
SELECT payload->>'instId' inst, payload->>'type' type, payload->>'subType' sub,
       payload->>'px' px, payload->>'pnl' pnl, payload->>'ccy' ccy,
       to_timestamp((payload->>'ts')::bigint/1000) AT TIME ZONE 'UTC' ts
FROM bronze.raw_bill
WHERE payload->>'type' = '3'          -- delivery/exercise
ORDER BY ts DESC LIMIT 20;

\echo '\n===== 7b. all distinct keys present on a delivery bill (to spot a settlement-price field) ====='
SELECT DISTINCT jsonb_object_keys(payload) key
FROM bronze.raw_bill WHERE payload->>'type' = '3' ORDER BY 1;

\echo '\n================ how to read this ================'
\echo 'S2: if position_snapshot.earliest is AFTER most deal_ledger expiries -> underlying_price'
\echo '    cannot cover them -> "no expiration price". Fix = ingest a real settlement price source'
\echo '    (OKX delivery/index-at-expiry) rather than deriving spot from snapshots.'
\echo 'S2 also shows if only 1-2 expiries have legs -> "only last expiration data".'
\echo 'S4: rows with px_rows=0 are exactly the expiries missing a settlement marker.'
