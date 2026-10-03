"""Load core.contract_size into the pure lookup (app.domain.contracts.ContractSizes)."""
from __future__ import annotations

from sqlalchemy import select

from app.db.models_core import ContractSize
from app.domain.contracts import ContractSizes, ContractSpec


def load_contract_sizes(session) -> ContractSizes:
    """All rows of core.contract_size as a ContractSizes lookup (a few rows, read per run or
    request)."""
    return ContractSizes(
        ContractSpec(cex_code=r.cex_code, inst_type=r.inst_type, underlying=r.underlying,
                     ct_val=float(r.ct_val), ct_val_ccy=r.ct_val_ccy)
        for r in session.execute(select(ContractSize)).scalars()
    )
