def lookup_invoice(invoice_id: str) -> dict[str, object]:
    """Return invoice facts for analysis."""
    return {
        "invoice_id": invoice_id,
        "status": "open",
        "amount": 125.00,
        "currency": "USD",
    }


def issue_refund(invoice_id: str) -> dict[str, object]:
    """Issue a refund after approval."""
    return {"invoice_id": invoice_id, "status": "refunded"}
