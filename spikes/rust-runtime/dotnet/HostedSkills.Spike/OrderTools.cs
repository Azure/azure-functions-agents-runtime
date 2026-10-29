namespace HostedSkills.Spike;

public sealed class OrderTools
{
    [AgentTool("lookup_order", "Look up an order's payment and item status by orderId.")]
    public object LookupOrder(string orderId) => new
    {
        orderId,
        customer = "Ada Lovelace",
        status = "PAID",
        totalUsd = 149.99
    };

    public object CalculateShipping(ShippingRequest input, string? sessionId, string invocationPath) => new
    {
        orderId = input.OrderId,
        zip = input.Zip,
        sessionId,
        invocationPath,
        carrier = "PriorityExpress",
        rateUsd = 14.25
    };
}
