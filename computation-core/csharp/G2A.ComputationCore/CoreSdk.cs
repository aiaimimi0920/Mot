using System.Globalization;
using System.Net.Http.Headers;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;

namespace G2A.ComputationCore;

/// <summary>
/// Tier 3 Computation Core SDK for .NET 8 / C# AI Agents (G2A V0.0.1).
/// Supports capability-digest caching, ClientWebSocket &amp; SSE event streams with Last-Event-ID cursor replay,
/// proactive refusal cooldown shield, early-push confirmation race safety, stale_context replanning,
/// and mandatory 504 outcome_unknown -> queue.query reconciliation.
/// </summary>
public sealed class CoreSdk : IAsyncDisposable
{
    public const string WireVersion = "0.0.1";
    public const int MaxSeenEventIds = 1024;

    private readonly ICognitivePlanner _planner;
    private readonly InMemoryCompanionStore? _memoryStore;
    private readonly bool _allowLoopback;
    private readonly string _streamMode; // "websocket" | "sse"
    private readonly HttpClient _http;
    private readonly object _gate = new();

    private readonly Dictionary<string, List<CapabilityEntry>> _capabilityCache = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _slotDigests = new(StringComparer.Ordinal);
    private readonly Dictionary<string, MethodCallQueue> _pendingConfirmQueues = new(StringComparer.Ordinal);
    private readonly HashSet<string> _earlyConfirmedIds = new(StringComparer.Ordinal);
    private readonly Dictionary<string, long> _earlyDeclinedIds = new(StringComparer.Ordinal);
    private readonly Dictionary<string, long> _refusalCooldowns = new(StringComparer.Ordinal);
    private readonly HashSet<string> _seenSet = new(StringComparer.Ordinal);
    private readonly Queue<string> _seenOrder = new();

    private string _hubEndpoint = "";
    private string _authToken = "";
    private FrozenScope? _frozenScope;
    private string _lastEventId = "";
    private long _nextAllowedPullMs;
    private CancellationTokenSource? _bgCts;
    private TaskCompletionSource<bool> _streamReady = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public string? SessionId { get; private set; }
    public bool ActionsPaused { get; private set; }
    public bool Closed { get; private set; }

    public Func<string, object?, string?, CancellationToken, Task<JsonElement>>? CustomPostTransport { get; set; }

    public CoreSdk(
        ICognitivePlanner? planner = null,
        InMemoryCompanionStore? memoryStore = null,
        bool allowLoopback = false,
        string streamMode = "websocket")
    {
        _planner = planner ?? new NoOpPlanner();
        _memoryStore = memoryStore;
        _allowLoopback = allowLoopback;
        _streamMode = streamMode;
        _http = new HttpClient(new SocketsHttpHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(12) };
    }

    public async Task<FrozenScope> ConnectAsync(
        string hubEndpoint,
        string authToken,
        string slotId,
        string? playerId = null,
        string? factionId = null,
        Dictionary<string, object?>? requestedScope = null,
        Dictionary<string, object?>? extensions = null,
        bool startBackgroundLoops = true,
        CancellationToken ct = default)
    {
        SsrfGuard.ValidateOutboundUrlAgainstSsrf(hubEndpoint, _allowLoopback);
        string baseEp = hubEndpoint.TrimEnd('/');
        string? cachedDigest;
        lock (_gate)
        {
            _slotDigests.TryGetValue(slotId, out cachedDigest);
            _hubEndpoint = baseEp;
            _authToken = authToken;
            Closed = false;
            _streamReady = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        }

        var payload = new Dictionary<string, object?>
        {
            ["slot_id"] = slotId,
            ["requested_scope"] = requestedScope ?? new Dictionary<string, object?> { ["allow_chat"] = true }
        };
        if (!string.IsNullOrEmpty(playerId)) payload["player_id"] = playerId;
        if (!string.IsNullOrEmpty(factionId)) payload["faction_id"] = factionId;
        if (!string.IsNullOrEmpty(cachedDigest)) payload["cached_capability_digest"] = cachedDigest;

        var resEl = await PostEnvelopeAsync("session.authorize", payload, null, extensions, ct);
        string sid = resEl.GetProperty("session_id").GetString()!;
        string capDigest = resEl.GetProperty("capability_table_digest").GetString()!;
        var scope = resEl.GetProperty("frozen_scope").Deserialize<FrozenScope>()!;

        if (resEl.TryGetProperty("capability_table", out var capTblEl) && capTblEl.ValueKind == JsonValueKind.Array)
        {
            var caps = ParseCapabilityTable(capTblEl);
            string verified = JcsDigest.ComputeSha256("g2a.cap.v1", capTblEl);
            if (verified != capDigest)
                throw new G2ACoreException("invalid_arguments", 400, "Mismatched capability_table_digest");
            lock (_gate) { _capabilityCache[capDigest] = caps; }
        }

        lock (_gate)
        {
            SessionId = sid;
            _frozenScope = scope;
            _slotDigests[slotId] = capDigest;
            _bgCts = new CancellationTokenSource();
        }

        if (startBackgroundLoops)
        {
            var bgToken = _bgCts.Token;
            _ = Task.Run(() => HeartbeatLoopAsync(bgToken), bgToken);
            if (_streamMode == "sse")
                _ = Task.Run(() => SseStreamLoopAsync(bgToken), bgToken);
            else
                _ = Task.Run(() => WebSocketStreamLoopAsync(bgToken), bgToken);
            await Task.WhenAny(_streamReady.Task, Task.Delay(1500, ct));
        }

        return scope;
    }

    public async Task<ProjectedContext> PullContextAsync(CancellationToken ct = default)
    {
        try
        {
            var el = await PostEnvelopeAsync("context.pull", new Dictionary<string, object?>(), SessionId, null, ct);
            return ParseProjectedContext(el);
        }
        catch (G2ACoreException gex) when (gex.Code == "resource_limit" && gex.RetryAfterMs.HasValue)
        {
            lock (_gate)
            {
                _nextAllowedPullMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() + gex.RetryAfterMs.Value;
            }
            throw;
        }
    }

    public async Task<ProjectedContext?> PullContextIfDueAsync(CancellationToken ct = default)
    {
        lock (_gate)
        {
            if (Closed || string.IsNullOrEmpty(SessionId) || DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() < _nextAllowedPullMs)
                return null;
        }
        try
        {
            return await PullContextAsync(ct);
        }
        catch (G2ACoreException gex) when (gex.Code == "resource_limit")
        {
            return null;
        }
    }

    public Task<QueueReceipt?> SubmitQueueWithRecoveryAsync(MethodCallQueue queue, CancellationToken ct = default) =>
        SubmitQueueInternalAsync(queue, 0, ct);

    private async Task<QueueReceipt?> SubmitQueueInternalAsync(MethodCallQueue queue, int depth, CancellationToken ct)
    {
        long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        string? fp = queue.Trigger == "proactive" ? JcsDigest.ComputeSha256("g2a.fp.v1", queue.Steps) : null;
        string? sid;
        lock (_gate)
        {
            if (Closed || string.IsNullOrEmpty(SessionId)) return null;
            sid = SessionId;
            foreach (var k in _refusalCooldowns.Where(kv => now >= kv.Value).Select(kv => kv.Key).ToList())
                _refusalCooldowns.Remove(k);
            if (fp != null && _refusalCooldowns.TryGetValue(fp, out long until) && now < until)
                return null;
        }

        try
        {
            var res = await PostEnvelopeAsync("queue.submit", queue, sid, null, ct);
            return res.Deserialize<QueueReceipt>();
        }
        catch (G2ACoreException gex)
        {
            if (gex.Code == "confirmation_required")
            {
                bool immediateResubmit = false;
                lock (_gate)
                {
                    if (_earlyConfirmedIds.Remove(queue.QueueId))
                    {
                        immediateResubmit = true;
                    }
                    else if (_earlyDeclinedIds.Remove(queue.QueueId, out long cdMs))
                    {
                        string declFp = JcsDigest.ComputeSha256("g2a.fp.v1", queue.Steps);
                        _refusalCooldowns[declFp] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() + cdMs;
                        return null;
                    }
                    else
                    {
                        _pendingConfirmQueues[queue.QueueId] = queue;
                    }
                }
                return immediateResubmit ? await SubmitQueueInternalAsync(queue, depth, ct) : null;
            }
            if (gex.Code == "stale_context" && gex.FreshContext != null && depth < 2)
                return await OnContextReceivedAsync(gex.FreshContext, depth + 1, ct);
            if (gex.Code == "outcome_unknown")
                return await QueryQueueAsync(queue.QueueId, ct);
            throw;
        }
    }

    public async Task<QueueReceipt?> QueryQueueAsync(string queueId, CancellationToken ct = default)
    {
        var el = await PostEnvelopeAsync("queue.query", new Dictionary<string, object?> { ["queue_id"] = queueId }, SessionId, null, ct);
        return el.Deserialize<QueueReceipt>();
    }

    public async Task<QueueReceipt?> CancelQueueAsync(string queueId, CancellationToken ct = default)
    {
        var el = await PostEnvelopeAsync("queue.cancel", new Dictionary<string, object?> { ["queue_id"] = queueId }, SessionId, null, ct);
        return el.Deserialize<QueueReceipt>();
    }

    public async Task SendChatAsync(string text, string audience = "player_only", CancellationToken ct = default)
    {
        await PostEnvelopeAsync("chat.send", new Dictionary<string, object?>
        {
            ["message_id"] = "msg-" + Guid.NewGuid().ToString("N")[..12],
            ["audience"] = audience,
            ["text"] = text,
            ["provenance"] = "ai_companion"
        }, SessionId, null, ct);
    }

    public async Task<QueueReceipt?> OnEventQueueConfirmedAsync(string queueId, CancellationToken ct = default)
    {
        MethodCallQueue? pending = null;
        lock (_gate)
        {
            if (_pendingConfirmQueues.Remove(queueId, out var q)) pending = q;
            else _earlyConfirmedIds.Add(queueId);
        }
        return pending != null ? await SubmitQueueWithRecoveryAsync(pending, ct) : null;
    }

    public void OnEventQueueDeclined(string queueId, long cooldownMs = 60000)
    {
        lock (_gate)
        {
            if (_pendingConfirmQueues.Remove(queueId, out var q))
            {
                string fp = JcsDigest.ComputeSha256("g2a.fp.v1", q.Steps);
                _refusalCooldowns[fp] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() + cooldownMs;
            }
            else
            {
                _earlyDeclinedIds[queueId] = cooldownMs;
            }
        }
    }

    public async Task ShutdownAsync(bool notifyHub = true)
    {
        string? sid;
        CancellationTokenSource? cts;
        lock (_gate)
        {
            if (Closed) return;
            Closed = true;
            sid = SessionId;
            cts = _bgCts;
        }
        cts?.Cancel();
        if (notifyHub && !string.IsNullOrEmpty(sid))
        {
            try
            {
                await PostEnvelopeAsync("session.close", new Dictionary<string, object?> { ["reason"] = "client_shutdown" }, sid, null, CancellationToken.None);
            }
            catch { }
        }
        cts?.Dispose();
    }

    public async ValueTask DisposeAsync()
    {
        await ShutdownAsync(false);
        _http.Dispose();
    }

    private async Task<QueueReceipt?> OnContextReceivedAsync(ProjectedContext ctx, int depth, CancellationToken ct)
    {
        List<CapabilityEntry> caps;
        string playerId;
        lock (_gate)
        {
            if (Closed || ActionsPaused || string.IsNullOrEmpty(SessionId)) return null;
            caps = _capabilityCache.TryGetValue(ctx.CapabilityTableDigest, out var c) ? c : new();
            playerId = _frozenScope?.PlayerId ?? "";
        }

        var recalled = _memoryStore != null && !string.IsNullOrEmpty(playerId)
            ? _memoryStore.RecallSafe(playerId, new SpoilerGuard(ctx.Epoch, "", ctx.AllowedTimelineTags, ctx.PlayerCommand ?? "", 48))
            : new List<MemoryRecord>();

        var decision = await _planner.PlanAsync(caps, ctx, recalled, ct);
        if (!string.IsNullOrEmpty(decision.ReplyText))
            await SendChatAsync(decision.ReplyText, decision.Audience, ct);

        if (decision.PlannedSteps is { Count: > 0 })
        {
            bool isCmd = !string.IsNullOrEmpty(ctx.PlayerCommand);
            var q = new MethodCallQueue(
                QueueId: "Q-" + Guid.NewGuid().ToString("N")[..12],
                Epoch: ctx.Epoch,
                BasedOnRev: ctx.Rev,
                Trigger: isCmd ? "player_command" : "proactive",
                Supersedes: isCmd,
                IntentSummary: decision.IntentSummary,
                Steps: decision.PlannedSteps);
            return await SubmitQueueInternalAsync(q, depth, ct);
        }
        return null;
    }

    private async Task HandlePushEnvelopeAsync(string rawJson, CancellationToken ct)
    {
        using var doc = JcsDigest.ParseStrictDocument(rawJson);
        var root = doc.RootElement;
        string evId = root.TryGetProperty("event_id", out var eid) && eid.ValueKind == JsonValueKind.String ? eid.GetString() ?? "" : "";
        if (!string.IsNullOrEmpty(evId))
        {
            lock (_gate)
            {
                if (!_seenSet.Add(evId)) return;
                _seenOrder.Enqueue(evId);
                _lastEventId = evId;
                while (_seenOrder.Count > MaxSeenEventIds)
                    _seenSet.Remove(_seenOrder.Dequeue());
            }
        }

        string evName = root.TryGetProperty("event", out var en) && en.ValueKind == JsonValueKind.String ? en.GetString() ?? "" : "";
        if (!root.TryGetProperty("payload", out var payload)) return;

        switch (evName)
        {
            case "context.push":
                lock (_gate) { _nextAllowedPullMs = 0; }
                await OnContextReceivedAsync(ParseProjectedContext(payload), 0, ct);
                break;
            case "capabilities.updated":
                if (payload.TryGetProperty("capability_table", out var tbl) && tbl.ValueKind == JsonValueKind.Array)
                {
                    string digest = payload.GetProperty("capability_table_digest").GetString() ?? "";
                    if (JcsDigest.ComputeSha256("g2a.cap.v1", tbl) == digest)
                    {
                        var parsedCaps = ParseCapabilityTable(tbl);
                        string sId = payload.TryGetProperty("slot_id", out var sl) ? sl.GetString() ?? "" : "";
                        lock (_gate)
                        {
                            _capabilityCache[digest] = parsedCaps;
                            if (!string.IsNullOrEmpty(sId)) _slotDigests[sId] = digest;
                        }
                    }
                }
                break;
            case "queue.confirmed":
                if (payload.TryGetProperty("queue_id", out var qidC) && qidC.GetString() is string qc)
                    await OnEventQueueConfirmedAsync(qc, ct);
                break;
            case "queue.declined":
                if (payload.TryGetProperty("queue_id", out var qidD) && qidD.GetString() is string qd)
                {
                    long cd = payload.TryGetProperty("cooldown_ms", out var cdEl) && cdEl.TryGetInt64(out long cVal) ? cVal : 60000;
                    OnEventQueueDeclined(qd, cd);
                }
                break;
            case "session.resumed":
                lock (_gate) { ActionsPaused = false; }
                await OnContextReceivedAsync(ParseProjectedContext(payload), 0, ct);
                break;
            case "session.closed":
                await ShutdownAsync(false);
                break;
        }
    }

    private async Task HeartbeatLoopAsync(CancellationToken ct)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(5));
        while (await timer.WaitForNextTickAsync(ct))
        {
            if (Closed || string.IsNullOrEmpty(SessionId)) return;
            try
            {
                await PostEnvelopeAsync("session.heartbeat", new Dictionary<string, object?>(), SessionId, null, ct);
            }
            catch (G2ACoreException gex) when (gex.Code is "revoked" or "permission_denied")
            {
                await ShutdownAsync(false);
                return;
            }
            catch { }
        }
    }

    private async Task WebSocketStreamLoopAsync(CancellationToken ct)
    {
        bool isReconnect = false;
        var buf = new byte[64 * 1024];
        while (!ct.IsCancellationRequested && !Closed)
        {
            string baseEp, tok, sid, lastId;
            lock (_gate) { baseEp = _hubEndpoint; tok = _authToken; sid = SessionId ?? ""; lastId = _lastEventId; }
            if (isReconnect)
            {
                try { await PostEnvelopeAsync("session.resume", new Dictionary<string, object?>(), sid, null, ct); } catch { }
            }
            using var ws = new ClientWebSocket();
            ws.Options.SetRequestHeader("Authorization", $"Bearer {tok}");
            if (!string.IsNullOrEmpty(lastId))
                ws.Options.SetRequestHeader("Last-Event-ID", lastId);
            string wsUrl = baseEp.Replace("https://", "wss://").Replace("http://", "ws://") +
                           $"/g2a/v1/core/ws?session_id={Uri.EscapeDataString(sid)}" +
                           (isReconnect ? "&replay=1" : "");
            try
            {
                await ws.ConnectAsync(new Uri(wsUrl), ct);
                _streamReady.TrySetResult(true);
                using var ms = new MemoryStream();
                while (ws.State == WebSocketState.Open && !ct.IsCancellationRequested)
                {
                    ms.SetLength(0);
                    WebSocketReceiveResult res;
                    do
                    {
                        res = await ws.ReceiveAsync(buf, ct);
                        if (res.MessageType == WebSocketMessageType.Close) goto Reconnect;
                        ms.Write(buf, 0, res.Count);
                    } while (!res.EndOfMessage);
                    if (res.MessageType == WebSocketMessageType.Text)
                        await HandlePushEnvelopeAsync(Encoding.UTF8.GetString(ms.GetBuffer(), 0, (int)ms.Length), ct);
                }
            }
            catch
            {
                if (ct.IsCancellationRequested || Closed) return;
            }
        Reconnect:
            isReconnect = true;
            try { await Task.Delay(150, ct); } catch { return; }
        }
    }

    private async Task SseStreamLoopAsync(CancellationToken ct)
    {
        bool isReconnect = false;
        while (!ct.IsCancellationRequested && !Closed)
        {
            string baseEp, tok, sid, lastId;
            lock (_gate) { baseEp = _hubEndpoint; tok = _authToken; sid = SessionId ?? ""; lastId = _lastEventId; }
            if (isReconnect)
            {
                try { await PostEnvelopeAsync("session.resume", new Dictionary<string, object?>(), sid, null, ct); } catch { }
            }
            string url = $"{baseEp}/g2a/v1/core/events?session_id={Uri.EscapeDataString(sid)}" + (isReconnect ? "&replay=1" : "");
            try
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, url);
                req.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("text/event-stream"));
                req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", tok);
                if (!string.IsNullOrEmpty(lastId)) req.Headers.TryAddWithoutValidation("Last-Event-ID", lastId);
                using var resp = await _http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct);
                resp.EnsureSuccessStatusCode();
                _streamReady.TrySetResult(true);
                await using var stream = await resp.Content.ReadAsStreamAsync(ct);
                using var reader = new StreamReader(stream, Encoding.UTF8);
                var dataLines = new List<string>();
                while (!ct.IsCancellationRequested)
                {
                    string? ln = await reader.ReadLineAsync(ct);
                    if (ln is null) break;
                    if (ln.Length == 0)
                    {
                        if (dataLines.Count > 0)
                        {
                            string joined = string.Join("\n", dataLines);
                            dataLines.Clear();
                            await HandlePushEnvelopeAsync(joined, ct);
                        }
                    }
                    else if (ln.StartsWith("data:", StringComparison.Ordinal))
                    {
                        dataLines.Add(ln[5..].TrimStart(' '));
                    }
                }
            }
            catch
            {
                if (ct.IsCancellationRequested || Closed) return;
            }
            isReconnect = true;
            try { await Task.Delay(150, ct); } catch { return; }
        }
    }

    private async Task<JsonElement> PostEnvelopeAsync(
        string operation,
        object? payload,
        string? sessionId,
        Dictionary<string, object?>? extensions,
        CancellationToken ct)
    {
        if (CustomPostTransport != null)
            return await CustomPostTransport(operation, payload, sessionId, ct);

        string baseEp, tok;
        lock (_gate) { baseEp = _hubEndpoint; tok = _authToken; }

        var env = new Dictionary<string, object?>
        {
            ["g2a_version"] = WireVersion,
            ["request_id"] = "cs-core-" + Guid.NewGuid().ToString("N")[..12],
            ["timestamp_ms"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            ["operation"] = operation,
            ["payload"] = payload ?? new Dictionary<string, object?>(),
            ["extensions"] = extensions ?? new Dictionary<string, object?>()
        };
        if (!string.IsNullOrEmpty(sessionId)) env["session_id"] = sessionId;

        using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseEp}/g2a/v1/core")
        {
            Content = new StringContent(JsonSerializer.Serialize(env), Encoding.UTF8, "application/json")
        };
        if (!string.IsNullOrEmpty(tok))
            req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", tok);

        using var resp = await _http.SendAsync(req, ct);
        byte[] raw = await resp.Content.ReadAsByteArrayAsync(ct);
        using var doc = JcsDigest.ParseStrictDocument(raw);
        var root = doc.RootElement;
        string acceptance = root.GetProperty("acceptance").GetString() ?? "not_accepted";
        if (acceptance == "accepted")
            return root.GetProperty("result").Clone();

        var errEl = root.GetProperty("error");
        string code = errEl.GetProperty("code").GetString() ?? "invalid_request";
        int httpStatus = errEl.TryGetProperty("http_status", out var hs) ? hs.GetInt32() : (int)resp.StatusCode;
        string msg = errEl.TryGetProperty("message", out var m) ? m.GetString() ?? "" : "";
        long? retryAfter = errEl.TryGetProperty("retry_after_ms", out var ra) && ra.ValueKind == JsonValueKind.Number ? ra.GetInt64() : null;
        if (!retryAfter.HasValue && resp.Headers.RetryAfter?.Delta is TimeSpan delta)
            retryAfter = (long)delta.TotalMilliseconds;
        long? cooldown = errEl.TryGetProperty("cooldown_ms", out var cd) && cd.ValueKind == JsonValueKind.Number ? cd.GetInt64() : null;
        ProjectedContext? fresh = errEl.TryGetProperty("fresh_context", out var fc) && fc.ValueKind == JsonValueKind.Object
            ? ParseProjectedContext(fc)
            : null;
        throw new G2ACoreException(code, httpStatus, msg, retryAfter, cooldown, fresh);
    }

    private static List<CapabilityEntry> ParseCapabilityTable(JsonElement arr)
    {
        var list = new List<CapabilityEntry>();
        foreach (var item in arr.EnumerateArray())
        {
            var schema = JcsDigest.ConvertElement(item.GetProperty("params_schema")) as Dictionary<string, object?> ?? new();
            var actors = item.GetProperty("permitted_actors").EnumerateArray().Select(x => x.GetString() ?? "").ToList();
            var flags = item.TryGetProperty("risk_flags", out var rf) && rf.ValueKind == JsonValueKind.Array
                ? rf.EnumerateArray().Select(x => x.GetString() ?? "").ToList()
                : new List<string>();
            list.Add(new CapabilityEntry(
                MethodId: item.GetProperty("method_id").GetString() ?? "",
                Name: item.GetProperty("name").GetString() ?? "",
                Description: item.GetProperty("description").GetString() ?? "",
                PermittedActors: actors,
                ParamsSchema: schema,
                Sustained: item.TryGetProperty("sustained", out var s) && s.ValueKind == JsonValueKind.True,
                RiskFlags: flags,
                DefaultPolicy: item.TryGetProperty("default_policy", out var dp) && dp.ValueKind == JsonValueKind.String ? dp.GetString() ?? "allow" : "allow"
            ));
        }
        return list;
    }

    private static ProjectedContext ParseProjectedContext(JsonElement el)
    {
        var brief = el.TryGetProperty("state_brief", out var sb) && sb.ValueKind == JsonValueKind.Object
            ? JcsDigest.ConvertElement(sb) as Dictionary<string, object?>
            : new Dictionary<string, object?>();
        var active = el.TryGetProperty("active_methods", out var am) && am.ValueKind == JsonValueKind.Array
            ? am.Deserialize<List<ActiveMethodGrant>>()
            : new List<ActiveMethodGrant>();
        Dictionary<string, Dictionary<string, object?>>? ext = null;
        if (el.TryGetProperty("extensions_data", out var ed) && ed.ValueKind == JsonValueKind.Object)
        {
            ext = new Dictionary<string, Dictionary<string, object?>>(StringComparer.Ordinal);
            foreach (var p in ed.EnumerateObject())
            {
                if (JcsDigest.ConvertElement(p.Value) is Dictionary<string, object?> sub)
                    ext[p.Name] = sub;
            }
        }
        return new ProjectedContext(
            SlotId: el.GetProperty("slot_id").GetString() ?? "",
            Epoch: el.GetProperty("epoch").GetInt64(),
            Rev: el.GetProperty("rev").GetInt64(),
            CapabilityTableDigest: el.GetProperty("capability_table_digest").GetString() ?? "",
            PlayerCommand: el.TryGetProperty("player_command", out var pc) && pc.ValueKind == JsonValueKind.String ? pc.GetString() : null,
            StateBrief: brief,
            ActiveMethods: active,
            ExtensionsData: ext
        );
    }
}
