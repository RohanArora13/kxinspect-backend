"""Deterministic, explicitly illustrative breakdowns for demo charges."""

from __future__ import annotations

from app.db.models import JsonDict


def default_cost_breakdown(amount_minor: int) -> list[JsonDict]:
    """Split a total into fake components without ever losing a minor unit."""
    tax_rate = 20
    pre_tax = amount_minor * 100 // (100 + tax_rate)
    materials = pre_tax * 46 // 100
    labour = pre_tax * 34 // 100
    service = pre_tax * 20 // 100
    tax = (pre_tax * tax_rate + 50) // 100
    rounding = amount_minor - materials - labour - service - tax
    return [
        {
            "label": "Materials and parts",
            "detail": "Illustrative replacement items and consumables",
            "amountMinor": materials,
        },
        {
            "label": "Technician labour",
            "detail": "Illustrative inspection, repair and fitting time",
            "amountMinor": labour,
        },
        {
            "label": "Service and handling",
            "detail": "Illustrative call-out and administration",
            "amountMinor": service,
        },
        {
            "label": "Service tax (demo)",
            "detail": "Illustrative 20% tax allocation",
            "amountMinor": tax,
        },
        {
            "label": "Rounding adjustment",
            "detail": "Adjustment so itemised amounts equal the displayed total",
            "amountMinor": rounding,
        },
    ]
