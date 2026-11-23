using System.Text.Json;
using G2A.InteractionOrigin;

sealed class RecordingTransport : IHubTransportClientPort
{
    public List<(string Op, object Payload)> Posts { get; } = new();
    public void PostToHub(string operation, object payload) => Posts.Add((operation, payload));
}

internal static class Program
{
    private static int Main(string[] args)
    {
        if (args.Length >= 2 && args[0] == "--live-hub")
        {
            string hubOriginUrl = args[1];
            string slotId = args.Length >= 4 && args[2] == "--slot" ? args[3] : "slot-csharp-live";
            return RunLiveHubHost(hubOriginUrl, slotId);
        }

        return RunUnitSelfTest();
    }

    private static int RunLiveHubHost(string hubOriginUrl, string slotId)
    {
        bool atChest = false;
        bool chestOpened = false;
        var chatReceived = new List<string>();

        using var tx = new HttpHubTransportClient(hubOriginUrl);
        var sdk = new OriginSdk(slotId, tx, chatUiHandler: (aud, txt, ai) => chatReceived.Add($"[{aud}|AI={ai}] {txt}"));
        using var listener = new LoopbackExecuteListener(sdk);

        sdk.BindActorSubject("companion", new object());
        sdk.BindActorSubject("player", new object());
        sdk.BindStateModule(
            "spatial_room",
            () => new Dictionary<string, object?> { ["chest_opened"] = chestOpened, ["player_holding_e_key"] = true },
            () =>
            {
                var list = new List<ActiveMethodGrant>();
                if (!chestOpened)
                {
                    list.Add(new ActiveMethodGrant("#01", "companion", "ready", new List<TargetCandidate>
                    {
                        new("chest_R", "Right Wooden Chest", "ready", null, "4.0m", "right 4.0m")
                    }));
                    list.Add(new ActiveMethodGrant("#02", "companion", atChest ? "ready" : "staged", new List<TargetCandidate>
                    {
                        new("chest_R", "Right Wooden Chest", atChest ? "ready" : "staged",
                            atChest ? null : "requires_proximity", atChest ? "0.5m" : "4.0m", "right 4.0m")
                    }));
                }
                return list;
            });

        sdk.RegisterCapability(
            new CapabilityEntry(
                "#01", "move_to", "Move companion to target",
                new List<string> { "companion" },
                new Dictionary<string, object?> { ["type"] = "object", ["required"] = new[] { "target_id" } },
                Sustained: true),
            (_, _, _, execId) =>
            {
                bool cancelled = false;
                _ = Task.Run(async () =>
                {
                    await Task.Delay(40);
                    if (!cancelled)
                    {
                        atChest = true;
                        sdk.CompleteStep(execId, true, new Dictionary<string, object?> { ["arrived"] = "chest_R" });
                    }
                });
                return new EffectInvocationResult.StartedExecuting(() => cancelled = true);
            });

        sdk.RegisterCapability(
            new CapabilityEntry(
                "#02", "interact", "Open target chest",
                new List<string> { "companion" },
                new Dictionary<string, object?> { ["type"] = "object", ["required"] = new[] { "target_id" } }),
            (_, _, stepArgs, _) =>
            {
                chestOpened = true;
                return new EffectInvocationResult.ImmediateSucceeded(new Dictionary<string, object?>
                {
                    ["opened"] = stepArgs["target_id"],
                    ["loot"] = new[] { "emerald_1", "gold_80" }
                });
            });

        sdk.PublishCapabilitiesAndBaseline(listener.ExecuteUrl);
        Console.WriteLine(JsonSerializer.Serialize(new Dictionary<string, object?>
        {
            ["status"] = "ready",
            ["slot_id"] = slotId,
            ["execute_url"] = listener.ExecuteUrl
        }));
        Console.Out.Flush();

        string? line;
        while ((line = Console.ReadLine()) != null)
        {
            line = line.Trim();
            if (line.StartsWith("CMD:", StringComparison.Ordinal))
            {
                sdk.ReportPlayerCommand(line[4..]);
            }
            else if (line == "STATUS")
            {
                Console.WriteLine(JsonSerializer.Serialize(new Dictionary<string, object?>
                {
                    ["at_chest"] = atChest,
                    ["chest_opened"] = chestOpened,
                    ["chat_count"] = chatReceived.Count
                }));
                Console.Out.Flush();
            }
            else if (line == "OFFLINE")
            {
                sdk.ReportPlayerPresence(false, 0);
                break;
            }
        }
        return 0;
    }

    private static int RunUnitSelfTest()
    {
        string d1 = JcsDigest.ComputeSha256("g2a.confirm.v1", new Dictionary<string, object?>
        {
            ["steps"] = new object[]
            {
                new Dictionary<string, object?>
                {
                    ["args"] = new Dictionary<string, object?> { ["qty"] = 20.0, ["item"] = "timber" },
                    ["step_index"] = 0
                }
            },
            ["queue_id"] = "Q-205",
            ["session_id"] = "sess-1"
        });
        const string golden = "sha256:8d8634fd72ae48e655a9151e50b06a3e1095e753cacb2c293706e59af1b9ccd8";
        if (d1 != golden) throw new Exception($"JCS mismatch: {d1} != {golden}");

        bool caughtDup = false;
        try { JcsDigest.ParseStrictDocument("{\"a\":1,\"a\":2}"); }
        catch (InvalidOperationException) { caughtDup = true; }
        if (!caughtDup) throw new Exception("Expected duplicate key rejection");

        var tx = new RecordingTransport();
        int gold = 500;
        bool cancelled = false;
        var sdk = new OriginSdk("slot-csharp", tx);
        sdk.BindActorSubject("companion", new object());
        sdk.BindActorSubject("player", new object());
        sdk.BindStateModule(
            "wallet",
            () => new Dictionary<string, object?> { ["gold"] = gold },
            () => new List<ActiveMethodGrant>
            {
                new("#01", "companion", "ready", new List<TargetCandidate> { new("chest_R", "Right Chest", "ready") }),
                new("#05", "player", "ready", new List<TargetCandidate>())
            });

        sdk.RegisterCapability(
            new CapabilityEntry("#01", "move_to", "Move", new List<string> { "companion" }, new(), Sustained: true),
            (_, _, _, _) => new EffectInvocationResult.StartedExecuting(() => cancelled = true));
        sdk.RegisterCapability(
            new CapabilityEntry("#05", "trade_resource", "Trade", new List<string> { "player" }, new()),
            (_, _, _, _) =>
            {
                gold -= 40;
                return new EffectInvocationResult.ImmediateSucceeded(new Dictionary<string, object?> { ["gold"] = gold });
            });

        sdk.PublishCapabilitiesAndBaseline();
        if (tx.Posts.Count != 2) throw new Exception("Expected capabilities + initial full sync");
        if (sdk.FlushDirtySync()) throw new Exception("Expected zero packet when state unchanged");

        var execRep = sdk.HandleOriginExecute(new OriginExecuteRequest("slot-csharp", 1, "invoke", "exec-cs-1", "#01", "companion", new()));
        if (execRep.State != "executing") throw new Exception("Expected executing state");

        var cancelRep = sdk.HandleOriginExecute(new OriginExecuteRequest("slot-csharp", 1, "cancel", "exec-cs-1", null, null, new()));
        if (!cancelled || cancelRep.State != "cancelled") throw new Exception("Expected cancellation");

        Console.WriteLine($"C# G2A.InteractionOrigin SelfTest PASSED | JCS={d1}");
        return 0;
    }
}
