"""Connector factory: map a `cex_code` to a concrete connector implementation."""
from __future__ import annotations

from app.connectors.base import BaseCexConnector, Credentials

# Registry of available connectors. Add new exchanges here.
_REGISTRY: dict[str, type[BaseCexConnector]] = {}


def register(cls: type[BaseCexConnector]) -> type[BaseCexConnector]:
    """Class decorator to register a connector under its `cex_code`."""
    _REGISTRY[cls.cex_code.upper()] = cls
    return cls


def make_connector(cex_code: str, credentials: Credentials) -> BaseCexConnector:
    """Instantiate the connector for `cex_code` (e.g. 'OKX')."""
    key = cex_code.upper()
    # Import implementations lazily so registration side-effects run.
    from app.connectors import okx  # noqa: F401  (registers OkxConnector)

    try:
        cls = _REGISTRY[key]
    except KeyError as exc:
        raise ValueError(
            f"No connector registered for CEX '{cex_code}'. Known: {sorted(_REGISTRY)}"
        ) from exc
    return cls(credentials)


def supported_cexes() -> list[str]:
    from app.connectors import okx  # noqa: F401
    return sorted(_REGISTRY)
