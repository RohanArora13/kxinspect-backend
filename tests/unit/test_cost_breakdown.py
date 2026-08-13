import pytest
from app.domain.cost_breakdown import default_cost_breakdown


@pytest.mark.parametrize("amount_minor", [0, 1, 100, 900, 2500, 3200])
def test_default_cost_breakdown_has_itemised_demo_tax_and_exact_rounding(
    amount_minor: int,
) -> None:
    breakdown = default_cost_breakdown(amount_minor)

    assert [item["label"] for item in breakdown] == [
        "Materials and parts",
        "Technician labour",
        "Service and handling",
        "Service tax (demo)",
        "Rounding adjustment",
    ]
    assert all(item["amountMinor"] >= 0 for item in breakdown)
    assert sum(item["amountMinor"] for item in breakdown) == amount_minor
