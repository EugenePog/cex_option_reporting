"""Fetch OPTION positions from the OKX account configured via OKX_K_* env vars.

This is a live call — it needs:
  * `python-okx` installed  (comes with `make install`)
  * the OKX_K_API_KEY / OKX_K_API_SECRET / OKX_K_PASSPHRASE / OKX_K_FLAG values in .env
  * network access to OKX

Run:
    python scripts/okx_positions.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running directly before `make install`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.connectors import make_connector  # noqa: E402
from config.settings import get_settings  # noqa: E402


def main() -> None:
    settings = get_settings()
    creds = settings.okx_k_credentials()

    if not creds.api_key:
        raise SystemExit(
            "OKX_K_API_KEY is empty. Add OKX_K_API_KEY / OKX_K_API_SECRET / "
            "OKX_K_PASSPHRASE / OKX_K_FLAG to your .env first."
        )

    conn = make_connector("OKX", creds)
    print(f"Connector: {conn.cex_code}  (flag={creds.flag!r} — '0' live, '1' demo)\n")

    positions = conn.fetch_positions(subacct="")  # single-account keys → subacct unused

    if not positions:
        print("No open OPTION positions returned.")
        return

    print(f"{len(positions)} open OPTION position(s):\n")
    print(f"{'instId':<28} {'side':<6} {'size':>10} {'avgPx':>12} {'markPx':>12} {'uPnL':>12}")
    print("-" * 84)
    for p in positions:
        mark = f"{p.mark_px:.6f}" if p.mark_px is not None else "-"
        print(f"{p.inst_id:<28} {p.side:<6} {p.size:>10.4f} {p.avg_px:>12.6f} {mark:>12} {p.upl:>12.4f}")


if __name__ == "__main__":
    main()
