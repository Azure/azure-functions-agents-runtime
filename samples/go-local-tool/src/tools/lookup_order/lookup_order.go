package main

func LookupOrder(orderID string) any {
	orders := map[string]any{
		"A-100": map[string]any{
			"status":             "processing",
			"estimated_delivery": "2026-10-12",
		},
		"A-200": map[string]any{
			"status":             "shipped",
			"estimated_delivery": "2026-10-10",
		},
	}
	if order, ok := orders[orderID]; ok {
		return order
	}
	return map[string]any{"status": "not_found"}
}
