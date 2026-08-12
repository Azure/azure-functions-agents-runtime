from __future__ import annotations


def summarize_order_quantities(items: list[dict[str, object]]) -> dict[str, int]:
    """Count line items and total integer quantities in an order."""
    quantities = [item.get("quantity", 0) for item in items]
    return {
        "line_items": len(items),
        "total_quantity": sum(
            quantity for quantity in quantities if isinstance(quantity, int)
        ),
    }
