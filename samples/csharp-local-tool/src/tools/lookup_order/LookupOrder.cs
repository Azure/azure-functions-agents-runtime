using System.Collections.Generic;

public static class OrderTools
{
    public static object LookupOrder(string order_id)
    {
        var orders = new Dictionary<string, object>
        {
            ["A-100"] = new { status = "processing", estimated_delivery = "2026-10-12" },
            ["A-200"] = new { status = "shipped", estimated_delivery = "2026-10-10" },
        };
        return orders.TryGetValue(order_id, out var order)
            ? order
            : new { status = "not_found" };
    }
}
