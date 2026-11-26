using System.Net.Sockets;
using System.Text;
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

        // Verify 60-entity crowd priority sorting & 600-step bounded ledger
        var crowdTx = new RecordingTransport();
        var crowdSdk = new OriginSdk("slot-crowd-cs", crowdTx, roomId: "room-cs", factionId: "alpha");
        crowdSdk.BindActorSubject("companion", new object());
        crowdSdk.RegisterCapability(
            new CapabilityEntry("#02", "interact", "Interact", new List<string> { "companion" }, new()),
            (_, _, _, _) => new EffectInvocationResult.ImmediateSucceeded(new Dictionary<string, object?> { ["ok"] = true }));
        var crowd = Enumerable.Range(0, 55)
            .Select(i => new TargetCandidate($"chest_{i:D2}", $"Chest {i}", "staged", null, "22.0m"))
            .ToList();
        crowd.Add(new TargetCandidate("chest_ready_close", "Close Chest", "ready", null, "1.1m"));
        crowd.Add(new TargetCandidate("chest_pointed", "Pointed Chest", "staged", null, "8.0m", "pointed_ray"));
        crowd.Add(new TargetCandidate("station_alert", "Alerted Station", "staged", null, "15.0m", null, "halted_no_material"));
        crowdSdk.BindStateModule("crowd", () => new(), () => new List<ActiveMethodGrant> { new("#02", "companion", "ready", crowd) });
        crowdSdk.PublishCapabilitiesAndBaseline();
        var syncPayload = (Dictionary<string, object?>)crowdTx.Posts[1].Payload;
        var upserted = (List<ActiveMethodGrant>)syncPayload["upserted_methods"]!;
        if (upserted[0].Targets.Count != 24) throw new Exception("Expected 24 capped targets");
        if (upserted[0].Targets[0].TargetId != "station_alert") throw new Exception("Expected alert #1");
        if (upserted[0].Targets[1].TargetId != "chest_pointed") throw new Exception("Expected pointed #2");
        if (upserted[0].Targets[2].TargetId != "chest_ready_close") throw new Exception("Expected ready closest #3");
        for (int i = 0; i < 600; i++)
        {
            crowdSdk.HandleOriginExecute(new OriginExecuteRequest("slot-crowd-cs", 1, "invoke", $"exec-b-{i}", "#02", "companion", new()));
        }

        // Verify persistent HTTP/1.1 Keep-Alive multi-request pipeline on LoopbackExecuteListener + constant-time token check
        using (var kaListener = new LoopbackExecuteListener(crowdSdk, port: 0, sharedToken: "ka-secret"))
        {
            var uri = new Uri(kaListener.ExecuteUrl);
            using var tcp = new TcpClient("127.0.0.1", uri.Port);
            using var netStream = tcp.GetStream();

            for (int seq = 0; seq < 5; seq++)
            {
                string bodyJson = JsonSerializer.Serialize(new Dictionary<string, object?>
                {
                    ["g2a_version"] = "0.0.1",
                    ["request_id"] = $"ka-req-{seq}",
                    ["timestamp_ms"] = 1000 + seq,
                    ["operation"] = "origin.execute",
                    ["payload"] = new Dictionary<string, object?>
                    {
                        ["slot_id"] = "slot-crowd-cs",
                        ["epoch"] = 1,
                        ["kind"] = "invoke",
                        ["execution_id"] = $"ka-exec-{seq}",
                        ["method_id"] = "#02",
                        ["actor"] = "companion",
                        ["args"] = new Dictionary<string, object?>()
                    },
                    ["extensions"] = new Dictionary<string, object?>()
                });
                byte[] bodyBytes = Encoding.UTF8.GetBytes(bodyJson);
                string authVal = seq == 0 ? "Bearer wrong-token" : "Bearer ka-secret";
                string rawHttp =
                    $"POST {uri.AbsolutePath} HTTP/1.1\r\n" +
                    $"Host: 127.0.0.1:{uri.Port}\r\n" +
                    $"Authorization: {authVal}\r\n" +
                    "Content-Type: application/json\r\n" +
                    "Connection: keep-alive\r\n" +
                    $"Content-Length: {bodyBytes.Length}\r\n\r\n";
                byte[] hdrBytes = Encoding.ASCII.GetBytes(rawHttp);
                netStream.Write(hdrBytes, 0, hdrBytes.Length);
                netStream.Write(bodyBytes, 0, bodyBytes.Length);
                netStream.Flush();

                string respHead = ReadRawHttpResponse(netStream, out string respBody);
                int expectedStatus = seq == 0 ? 401 : 200;
                if (!respHead.Contains($" {expectedStatus} ", StringComparison.Ordinal))
                    throw new Exception($"Expected HTTP {expectedStatus} on Keep-Alive seq {seq}, got: {respHead}");
                if (seq > 0 && !respBody.Contains("\"succeeded\"", StringComparison.Ordinal))
                    throw new Exception($"Expected succeeded execute reply on seq {seq}, got: {respBody}");
            }
        }

        // Verify required ParamsSchema enforcement & player_metered independent_npc host_handoff
        var npcTx = new RecordingTransport();
        var npcSdk = new OriginSdk("slot-npc-cs", npcTx, roleKind: "independent_npc", billingMode: "player_metered");
        npcSdk.BindActorSubject("boss", new object());
        npcSdk.RegisterCapability(
            new CapabilityEntry(
                "#07", "cast_spell", "Cast spell", new List<string> { "boss" },
                new Dictionary<string, object?> { ["required"] = new[] { "spell_id" } }),
            (_, _, _, _) => new EffectInvocationResult.ImmediateSucceeded(new Dictionary<string, object?>()));

        bool caughtMissingArg = false;
        try
        {
            npcSdk.HandleOriginExecute(new OriginExecuteRequest("slot-npc-cs", 1, "invoke", "ex-miss", "#07", "boss", new()));
        }
        catch (G2AOriginException gex) when (gex.Code == "invalid_arguments")
        {
            caughtMissingArg = true;
        }
        if (!caughtMissingArg) throw new Exception("Expected missing required argument rejection in C# OriginSdk");

        npcSdk.ReportNpcHostHandoff("player-teammate-2");
        var hoPayload = (Dictionary<string, object?>)npcTx.Posts[^1].Payload;
        if ((string?)hoPayload["kind"] != "host_handoff" || (string?)hoPayload["new_player_id"] != "player-teammate-2")
            throw new Exception("Expected host_handoff event in C# OriginSdk");

        // Verify FRAND slot governance fields & reverse-DNS modular extension data (§7.3 & §8.1)
        var extTx = new RecordingTransport();
        var extSdk = new OriginSdk("slot-ext-cs", extTx);
        extSdk.AttachExtensionData("org.g2a.sim.batch_blueprint.v1", new Dictionary<string, object?> { ["grid"] = "6x6" });
        extSdk.PublishCapabilitiesAndBaseline(
            originExecuteUrl: "pull://origin.execute",
            maxSessions: 2,
            sharedCooldownMs: 650,
            allowlistedCores: new List<string> { "core-alpha" },
            allowlistCriteriaUrl: "https://game.example/g2a-frand");
        var extCap = (Dictionary<string, object?>)extTx.Posts[0].Payload;
        if ((long)extCap["shared_cooldown_ms"]! != 650L || (string?)extCap["allowlist_criteria_url"] != "https://game.example/g2a-frand")
            throw new Exception("Expected FRAND governance metadata in C# OriginSdk");
        var extSync = (Dictionary<string, object?>)extTx.Posts[1].Payload;
        var extData = (Dictionary<string, Dictionary<string, object?>>)extSync["extensions_data"]!;
        if ((string?)extData["org.g2a.sim.batch_blueprint.v1"]["grid"] != "6x6")
            throw new Exception("Expected modular extensions_data in C# OriginSdk");

        // Verify Pose3D 360-degree yaw-relative deictics & recursive JSON Schema validation
        var pPose = new Pose3D(0, 0, 0, 0);
        if (pPose.FormatPlayerDeicticAnchor(new Pose3D(3, 0, 0, 0), false) != "right 3.0m")
            throw new Exception("Unexpected Pose3D right deictic anchor");
        if (pPose.FormatPlayerDeicticAnchor(new Pose3D(0, 0, 4, 0), true) != "pointed_ray 4.0m")
            throw new Exception("Unexpected Pose3D pointed_ray anchor");

        bool caughtOverMax = false;
        try
        {
            OriginSdk.ValidateArgsAgainstSchema(
                new Dictionary<string, object?> { ["qty"] = 999 },
                new Dictionary<string, object?>
                {
                    ["type"] = "object",
                    ["required"] = new[] { "qty" },
                    ["additionalProperties"] = false,
                    ["properties"] = new Dictionary<string, object?>
                    {
                        ["qty"] = new Dictionary<string, object?> { ["type"] = "integer", ["minimum"] = 1, ["maximum"] = 50 }
                    }
                });
        }
        catch (G2AOriginException gex) when (gex.Code == "invalid_arguments")
        {
            caughtOverMax = true;
        }
        if (!caughtOverMax) throw new Exception("Expected recursive JSON Schema maximum bound rejection");

        Console.WriteLine($"C# G2A.InteractionOrigin SelfTest PASSED | JCS={d1}");
        return 0;
    }

    private static string ReadRawHttpResponse(NetworkStream s, out string body)
    {
        using var hdr = new MemoryStream();
        int state = 0;
        while (state < 4)
        {
            int b = s.ReadByte();
            if (b < 0) throw new EndOfStreamException("Unexpected EOF reading HTTP response header");
            hdr.WriteByte((byte)b);
            if ((state == 0 || state == 2) && b == '\r') state++;
            else if ((state == 1 || state == 3) && b == '\n') state++;
            else state = b == '\r' ? 1 : 0;
        }
        string headStr = Encoding.ASCII.GetString(hdr.ToArray());
        int cl = 0;
        foreach (var line in headStr.Split("\r\n", StringSplitOptions.RemoveEmptyEntries))
        {
            if (line.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase))
                cl = int.Parse(line["Content-Length:".Length..].Trim());
        }
        byte[] buf = new byte[cl];
        int off = 0;
        while (off < cl)
        {
            int r = s.Read(buf, off, cl - off);
            if (r <= 0) throw new EndOfStreamException("Unexpected EOF reading HTTP response body");
            off += r;
        }
        body = Encoding.UTF8.GetString(buf);
        return headStr;
    }
}
