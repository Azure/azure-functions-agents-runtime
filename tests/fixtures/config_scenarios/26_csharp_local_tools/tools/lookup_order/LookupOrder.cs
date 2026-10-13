public static class OrderTools
{
    public static object LookupOrder(string order_id)
    {
        return new { order_id, status = "processing" };
    }
}
