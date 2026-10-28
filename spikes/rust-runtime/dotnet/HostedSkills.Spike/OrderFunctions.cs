using System.Net;
using System.Text.Json;
using Microsoft.Azure.Functions.Worker;
using Microsoft.Azure.Functions.Worker.Http;

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

public sealed record SupportRequest(string Prompt, string? SessionId);

public sealed record ShippingRequest
{
    public required string OrderId { get; init; }
    public required string Zip { get; init; }
}

public sealed class OrderFunctions(HostedSkill skill, RuntimeService runtime)
{
    [Function("HostedSkillSupport")]
    public async Task<HttpResponseData> SupportAsync(
        [HttpTrigger(AuthorizationLevel.Anonymous, "post", Route = "support")] HttpRequestData request,
        CancellationToken cancellationToken)
    {
        var input = await JsonSerializer.DeserializeAsync<SupportRequest>(
            request.Body, new JsonSerializerOptions(JsonSerializerDefaults.Web), cancellationToken);
        if (string.IsNullOrWhiteSpace(input?.Prompt))
        {
            var badRequest = request.CreateResponse(HttpStatusCode.BadRequest);
            await badRequest.WriteAsJsonAsync(new { error = "prompt is required" }, cancellationToken);
            return badRequest;
        }
        var result = await skill.RunAsync(input.Prompt, input.SessionId, cancellationToken);
        var session = await runtime.Client.InspectSessionAsync(
            skill.AgentName, result.SessionId, cancellationToken);
        var response = request.CreateResponse(HttpStatusCode.OK);
        await response.WriteAsJsonAsync(new { inference = "MOCK", result, session }, cancellationToken);
        return response;
    }

}

public sealed class ShippingFunctions(OrderTools tools)
{
    [Function("ShippingQuote")]
    [AgentTool("calculate_shipping_http", "Calculate shipping for an order and destination ZIP.",
        InputType = typeof(ShippingRequest))]
    public async Task<HttpResponseData> ShippingQuoteAsync(
        [HttpTrigger(AuthorizationLevel.Anonymous, "post", Route = "shipping/quote")] HttpRequestData request,
        CancellationToken cancellationToken = default)
    {
        ShippingRequest? input;
        try
        {
            input = await JsonSerializer.DeserializeAsync<ShippingRequest>(
                request.Body, JsonSerializerOptions.Web, cancellationToken);
        }
        catch (JsonException)
        {
            return await BadRequestAsync(request, cancellationToken);
        }
        if (input is null || string.IsNullOrWhiteSpace(input.OrderId) || string.IsNullOrWhiteSpace(input.Zip))
            return await BadRequestAsync(request, cancellationToken);

        var sessionId = request.Headers.TryGetValues("x-fhs-session-id", out var values)
            ? values.FirstOrDefault() : null;
        var directToolCall = request.FunctionContext.Items.TryGetValue(nameof(AgentToolAttribute), out var marker)
            && marker is true;
        var result = tools.CalculateShipping(
            input, sessionId, directToolCall ? "stdio-function-handler" : "http-trigger");
        var response = request.CreateResponse(HttpStatusCode.OK);
        await response.WriteAsJsonAsync(result, cancellationToken);
        return response;
    }

    private static async Task<HttpResponseData> BadRequestAsync(
        HttpRequestData request, CancellationToken cancellationToken)
    {
        var response = request.CreateResponse(HttpStatusCode.BadRequest);
        await response.WriteAsJsonAsync(new { error = "orderId and zip must be nonempty strings" }, cancellationToken);
        return response;
    }
}
