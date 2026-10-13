export function lookup_order(order_id) {
  const orders = {
    "A-100": { status: "processing", estimated_delivery: "2026-10-12" },
    "A-200": { status: "shipped", estimated_delivery: "2026-10-10" },
  };
  return orders[order_id] ?? { status: "not_found" };
}

