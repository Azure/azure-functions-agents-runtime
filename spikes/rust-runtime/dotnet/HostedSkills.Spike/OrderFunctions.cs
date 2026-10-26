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

    public object CalculateShipping(JsonElement input, string? sessionId, string invocationPath) => new
    {
        orderId = input.GetProperty("orderId").GetString(),
        zip = input.GetProperty("zip").GetString(),
        sessionId,
        invocationPath,
        carrier = "PriorityExpress",
        rateUsd = 14.25
    };
}

public sealed record SupportRequest(string Prompt, string? SessionId);

public sealed class OrderFunctions(HostedSkill skill, OrderTools tools, RuntimeService runtime)
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

    [Function("ShippingQuote")]
    public async Task<HttpResponseData> ShippingQuoteAsync(
        [HttpTrigger(AuthorizationLevel.Anonymous, "post", Route = "shipping/quote")] HttpRequestData request,
        CancellationToken cancellationToken)
    {
        using var body = await JsonDocument.ParseAsync(request.Body, cancellationToken: cancellationToken);
        var sessionId = request.Headers.TryGetValues("x-fhs-session-id", out var values)
            ? values.FirstOrDefault() : null;
        var result = tools.CalculateShipping(body.RootElement, sessionId, "http-trigger");
        var response = request.CreateResponse(HttpStatusCode.OK);
        await response.WriteAsJsonAsync(result, cancellationToken);
        return response;
    }
}
