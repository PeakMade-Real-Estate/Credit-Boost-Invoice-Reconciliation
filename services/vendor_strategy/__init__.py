"""
Vendor strategy registry and factory.

Usage::

    from services.vendor_strategy import get_vendor_strategy

    strategy = get_vendor_strategy("rent_plus")
    # or
    strategy = get_vendor_strategy("Rent Plus")

To add a new vendor:

1. Implement ``VendorReconciliationStrategy`` in a new module.
2. Add it to ``_STRATEGY_REGISTRY`` below.
3. Add any display-name aliases to ``_VENDOR_CODE_ALIASES``.
"""
from __future__ import annotations

from services.vendor_strategy.base import VendorReconciliationStrategy
from services.vendor_strategy.boom_strategy import BoomReconciliationStrategy
from services.vendor_strategy.rentplus_strategy import RentPlusReconciliationStrategy

__all__ = [
    "VendorReconciliationStrategy",
    "RentPlusReconciliationStrategy",
    "BoomReconciliationStrategy",
    "get_vendor_strategy",
    "normalize_vendor_code",
]

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_STRATEGY_REGISTRY: dict = {
    "rent_plus": RentPlusReconciliationStrategy,
    "credit_boost_boom": BoomReconciliationStrategy,
}

# Alternate display-name forms that map to canonical codes
_VENDOR_CODE_ALIASES: dict = {
    "rent plus": "rent_plus",
    "rentplus": "rent_plus",
    "credit boost powered by boom": "credit_boost_boom",
    "credit boost - boom": "credit_boost_boom",
    "credit boost boom": "credit_boost_boom",
    "boom": "credit_boost_boom",
    "credit boost": "credit_boost_boom",
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def normalize_vendor_code(vendor: str) -> str:
    """Convert a vendor name or code to its canonical ``snake_case`` form.

    Args:
        vendor: Display name (``"Rent Plus"``) or code (``"rent_plus"``).

    Returns:
        Canonical vendor code string.

    Examples::

        >>> normalize_vendor_code("Rent Plus")
        'rent_plus'
        >>> normalize_vendor_code("Credit Boost powered by Boom")
        'credit_boost_boom'
    """
    key = vendor.strip().lower()
    return _VENDOR_CODE_ALIASES.get(key, key.replace(" ", "_").replace("-", "_"))


def get_vendor_strategy(vendor_code: str) -> VendorReconciliationStrategy:
    """Return an instantiated strategy for *vendor_code*.

    Args:
        vendor_code: Canonical code (e.g. ``"rent_plus"``) or display name
                     (e.g. ``"Rent Plus"``).  Case-insensitive.

    Returns:
        A ready-to-use ``VendorReconciliationStrategy`` instance.

    Raises:
        ValueError: If no strategy is registered for *vendor_code*.
    """
    canonical = normalize_vendor_code(vendor_code)
    cls = _STRATEGY_REGISTRY.get(canonical)
    if cls is None:
        available = sorted(_STRATEGY_REGISTRY.keys())
        raise ValueError(
            f"Unknown vendor code {vendor_code!r}.  "
            f"Available vendors: {available}"
        )
    return cls()
