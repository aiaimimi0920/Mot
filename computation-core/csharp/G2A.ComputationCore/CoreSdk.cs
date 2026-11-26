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
    private readonly HashSet<string> _confirmHoldIds = new(StringComparer.Ordinal);
    private readonly HashSet<string> _earlyConfirmedIds = new(StringComparer.Ordinal);
    private readonly Dictionary<string, long> _earlyDeclinedIds = new(StringComparer.Ordinal);
    private readonly HashSet<string> _declinedQueueIds = new(StringComparer.Ordinal);
    private readonly Dictionary<string, QueueReceipt> _confirmedReceipts = new(StringComparer.Ordinal);
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
    public FrozenScope? CurrentScope { get { lock (_gate) return _frozenScope; } }
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

    public static async Task<CoreSdk> ConnectQuickAsync(
        string slotId,
        string authToken,
        ICognitivePlanner? planner = null,
        string hubEndpoint = "auto",
        string? fallbackUrl = null,
        string streamMode = "websocket",
        bool allowLoopback = true,
        InMemoryCompanionStore? memoryStore = null,
        Dictionary<string, object?>? extensions = null,
        CancellationToken ct = default)
    {
        string resolved = hubEndpoint;
        if (hubEndpoint == "auto")
        {
            try { resolved = HubDiscovery.DiscoverLocalHubCoreUrl(); }
            catch when (!string.IsNullOrEmpty(fallbackUrl)) { resolved = fallbackUrl!; }
        }
        var sdk = new CoreSdk(planner, memoryStore, allowLoopback, streamMode);
        await sdk.ConnectAsync(resolved, authToken, slotId, extensions: extensions, ct: ct);
        return sdk;
    }

    public QueueBuilder CreateQueueBuilder(
        ProjectedContext context,
        string intentSummary = "Execute planned companion queue",
        string? trigger = null) => new(this, context, intentSummary, trigger);

    public IReadOnlyList<CapabilityEntry> GetCachedCapabilities(string? slotId = null)
    {
        lock (_gate)
        {
            string targetSlot = slotId ?? _frozenScope?.SlotId ?? "";
            string digest = _slotDigests.TryGetValue(targetSlot, out var d) ? d : (_frozenScope?.CapabilityTableDigest ?? "");
            return _capabilityCache.TryGetValue(digest, out var c) ? c.ToList() : Array.Empty<CapabilityEntry>();
        }
    }

    public async Task<long> SendHeartbeatAsync(CancellationToken ct = default)
    {
        var el = await PostEnvelopeAsync("session.heartbeat", new Dictionary<string, object?>(), SessionId, null, ct);
        return el.TryGetProperty("lease_deadline_ms", out var ld) && ld.TryGetInt64(out long ms) ? ms : 0L;
    }

    public async Task<bool> ResumeSessionAsync(CancellationToken ct = default)
    {
        var el = await PostEnvelopeAsync("session.resume", new Dictionary<string, object?>(), SessionId, null, ct);
        bool paused = el.TryGetProperty("actions_paused", out var ap) && ap.ValueKind == JsonValueKind.True;
        lock (_gate) { ActionsPaused = paused; }
        return paused;
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
                    _confirmHoldIds.Add(queue.QueueId);
                    if (_earlyConfirmedIds.Remove(queue.QueueId))
                    {
                        immediateResubmit = true;
                    }
                    else if (_earlyDeclinedIds.Remove(queue.QueueId, out long cdMs))
                    {
                        _declinedQueueIds.Add(queue.QueueId);
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
        if (pending == null) return null;
        var rec = await SubmitQueueWithRecoveryAsync(pending, ct);
        if (rec != null)
        {
            lock (_gate) { _confirmedReceipts[queueId] = rec; }
        }
        return rec;
    }

    public void OnEventQueueDeclined(string queueId, long cooldownMs = 60000)
    {
        lock (_gate)
        {
            _declinedQueueIds.Add(queueId);
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

    public async Task<QueueReceipt?> WaitForQueueTerminalAsync(
        string queueId,
        QueueReceipt? initialReceipt = null,
        CancellationToken ct = default)
    {
        static bool IsNonTerminal(string? st) => st is "running" or "executing" or "awaiting_confirmation";
        var rec = initialReceipt;
        if (rec != null && !IsNonTerminal(rec.QueueState)) return rec;

        while (!ct.IsCancellationRequested && !Closed)
        {
            bool everHeld, declined;
            QueueReceipt? confRec;
            lock (_gate)
            {
                everHeld = _confirmHoldIds.Contains(queueId);
                declined = _declinedQueueIds.Contains(queueId);
                _confirmedReceipts.TryGetValue(queueId, out confRec);
            }
            if (declined)
                return new QueueReceipt(queueId, "cancelled", new List<StepReceipt>());
            if (confRec != null)
            {
                if (!IsNonTerminal(confRec.QueueState)) return confRec;
                rec = confRec;
            }
            if (initialReceipt == null && !everHeld) return null;
            if (everHeld && confRec == null)
            {
                await Task.Delay(35, ct);
                continue;
            }
            try
            {
                rec = await QueryQueueAsync(queueId, ct);
            }
            catch (G2ACoreException)
            {
                break;
            }
            if (rec != null && !IsNonTerminal(rec.QueueState)) return rec;
            await Task.Delay(35, ct);
        }
        return rec;
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

    public async Task<GoalExecutionReport> ExecuteGoalAsync(
        string objective,
        Func<ProjectedContext, QueueReceipt?, bool>? successWhen = null,
        int maxTurns = 6,
        CancellationToken ct = default)
    {
        if (maxTurns <= 0) maxTurns = 6;
        string goalId = "goal-" + Guid.NewGuid().ToString("N")[..10];
        var failedFps = new HashSet<string>(StringComparer.Ordinal);
        var submitted = new List<string>();
        QueueReceipt? lastRec = null;
        ProjectedContext? lastCtx = null;
        int turns = 0;

        while (!ct.IsCancellationRequested && !Closed && !ActionsPaused && turns < maxTurns)
        {
            var pulled = await PullContextIfDueAsync(ct) ?? await PullContextAsync(ct);
            lastCtx = pulled;
            if (successWhen != null && successWhen(pulled, lastRec))
                return new GoalExecutionReport(goalId, objective, true, "completed", turns, submitted, pulled);

            var synth = pulled with { PlayerCommand = objective };
            var caps = GetCachedCapabilities(synth.SlotId);
            var dec = await _planner.PlanAsync(caps, synth, Array.Empty<MemoryRecord>(), ct);
            if (!string.IsNullOrEmpty(dec.ReplyText))
                await SendChatAsync(dec.ReplyText, dec.Audience, ct);

            var filtered = new List<QueueStep>();
            if (dec.PlannedSteps != null)
            {
                foreach (var s in dec.PlannedSteps)
                {
                    string fp = JcsDigest.ComputeSha256("g2a.agent.loop.v1", new Dictionary<string, object?>
                    {
                        ["brief"] = synth.StateBrief,
                        ["method_id"] = s.MethodId,
                        ["actor"] = s.Actor,
                        ["args"] = s.Args
                    });
                    if (failedFps.Contains(fp)) break;
                    filtered.Add(new QueueStep(filtered.Count, s.MethodId, s.Actor, s.Args));
                }
            }

            if (filtered.Count == 0)
            {
                string st = dec.GoalStatus == "blocked" ? "blocked" : "no_executable_steps";
                return new GoalExecutionReport(goalId, objective, false, st, turns, submitted, pulled, st);
            }

            turns++;
            string qid = "Q-" + Guid.NewGuid().ToString("N")[..12];
            submitted.Add(qid);
            var initialRec = await SubmitQueueWithRecoveryAsync(new MethodCallQueue(
                QueueId: qid,
                Epoch: synth.Epoch,
                BasedOnRev: synth.Rev,
                Trigger: "player_command",
                Supersedes: turns == 1,
                IntentSummary: string.IsNullOrEmpty(dec.IntentSummary) ? objective : dec.IntentSummary,
                Steps: filtered), ct);
            lastRec = await WaitForQueueTerminalAsync(qid, initialRec, ct);
            if (initialRec == null && lastRec?.QueueState == "cancelled")
                return new GoalExecutionReport(goalId, objective, false, "declined_by_player", turns, submitted, lastCtx, "declined_by_player");

            if (lastRec != null)
            {
                foreach (var sr in lastRec.StepReceipts)
                {
                    if (sr.State == "failed" && sr.StepIndex >= 0 && sr.StepIndex < filtered.Count)
                    {
                        var st = filtered[sr.StepIndex];
                        failedFps.Add(JcsDigest.ComputeSha256("g2a.agent.loop.v1", new Dictionary<string, object?>
                        {
                            ["brief"] = synth.StateBrief,
                            ["method_id"] = st.MethodId,
                            ["actor"] = st.Actor,
                            ["args"] = st.Args
                        }));
                    }
                }
            }

            var fresh = await PullContextIfDueAsync(ct);
            if (fresh != null) lastCtx = fresh;
            if (successWhen != null && lastCtx != null && successWhen(lastCtx, lastRec))
                return new GoalExecutionReport(goalId, objective, true, "completed", turns, submitted, lastCtx);
            if (successWhen == null && lastRec?.QueueState == "completed" && dec.GoalStatus != "continue")
                return new GoalExecutionReport(goalId, objective, true, "completed", turns, submitted, lastCtx);

            await Task.Delay(25, ct);
        }

        return new GoalExecutionReport(goalId, objective, false, "exhausted", turns, submitted, lastCtx, "max_turns_exceeded");
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
        int attempt = 0;
        var buf = new byte[64 * 1024];
        while (!ct.IsCancellationRequested && !Closed)
        {
            string baseEp, tok, sid, lastId;
            lock (_gate) { baseEp = _hubEndpoint; tok = _authToken; sid = SessionId ?? ""; lastId = _lastEventId; }
            if (isReconnect)
            {
                try
                {
                    await ResumeSessionAsync(ct);
                }
                catch (G2ACoreException gex) when (gex.Code is "revoked" or "permission_denied")
                {
                    await ShutdownAsync(false);
                    return;
                }
                catch { }
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
                attempt = 0;
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
            int backoffMs = Math.Min(150 * (1 << Math.Min(attempt++, 5)), 4000);
            try { await Task.Delay(backoffMs, ct); } catch { return; }
        }
    }

    private async Task SseStreamLoopAsync(CancellationToken ct)
    {
        bool isReconnect = false;
        int attempt = 0;
        while (!ct.IsCancellationRequested && !Closed)
        {
            string baseEp, tok, sid, lastId;
            lock (_gate) { baseEp = _hubEndpoint; tok = _authToken; sid = SessionId ?? ""; lastId = _lastEventId; }
            if (isReconnect)
            {
                try
                {
                    await ResumeSessionAsync(ct);
                }
                catch (G2ACoreException gex) when (gex.Code is "revoked" or "permission_denied")
                {
                    await ShutdownAsync(false);
                    return;
                }
                catch { }
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
                attempt = 0;
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
            int backoffMs = Math.Min(150 * (1 << Math.Min(attempt++, 5)), 4000);
            try { await Task.Delay(backoffMs, ct); } catch { return; }
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

        HttpResponseMessage resp;
        byte[] raw;
        try
        {
            resp = await _http.SendAsync(req, ct);
            raw = await resp.Content.ReadAsByteArrayAsync(ct);
        }
        catch (TaskCanceledException tex) when (!ct.IsCancellationRequested)
        {
            throw new G2ACoreException("outcome_unknown", 504, $"Request timed out: {tex.Message}");
        }
        using (resp)
        {
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
        var visual = el.TryGetProperty("visual_ref", out var vr) && vr.ValueKind == JsonValueKind.Object
            ? JcsDigest.ConvertElement(vr) as Dictionary<string, object?>
            : null;
        var audio = el.TryGetProperty("audio_ref", out var ar) && ar.ValueKind == JsonValueKind.Object
            ? JcsDigest.ConvertElement(ar) as Dictionary<string, object?>
            : null;
        var timelineTags = el.TryGetProperty("allowed_timeline_tags", out var tt) && tt.ValueKind == JsonValueKind.Array
            ? tt.EnumerateArray().Select(x => x.GetString() ?? "").ToList()
            : null;
        return new ProjectedContext(
            SlotId: el.GetProperty("slot_id").GetString() ?? "",
            Epoch: el.GetProperty("epoch").GetInt64(),
            Rev: el.GetProperty("rev").GetInt64(),
            CapabilityTableDigest: el.GetProperty("capability_table_digest").GetString() ?? "",
            PlayerCommand: el.TryGetProperty("player_command", out var pc) && pc.ValueKind == JsonValueKind.String ? pc.GetString() : null,
            StateBrief: brief,
            ActiveMethods: active,
            VisualRef: visual,
            AudioRef: audio,
            AllowedTimelineTags: timelineTags,
            ExtensionsData: ext
        );
    }

    internal IReadOnlyList<CapabilityEntry> GetCachedCapabilitiesByDigest(string digest)
    {
        lock (_gate)
            return _capabilityCache.TryGetValue(digest, out var c) ? c : Array.Empty<CapabilityEntry>();
    }
}

/// <summary>
/// Fluent queue builder performing zero-latency client-side pre-flight checks against FrozenScope, ActiveMethods, and ParamsSchema.
/// </summary>
public sealed class QueueBuilder
{
    private readonly CoreSdk _sdk;
    private readonly ProjectedContext _ctx;
    private readonly string _intentSummary;
    private readonly string _trigger;
    private readonly bool _supersedes;
    private readonly List<QueueStep> _steps = new();
    private readonly Dictionary<string, ActiveMethodGrant> _grants;
    private readonly Dictionary<string, CapabilityEntry> _caps;

    internal QueueBuilder(CoreSdk sdk, ProjectedContext ctx, string intentSummary, string? trigger)
    {
        _sdk = sdk;
        _ctx = ctx;
        _intentSummary = intentSummary;
        bool isCmd = !string.IsNullOrEmpty(ctx.PlayerCommand);
        _trigger = trigger ?? (isCmd ? "player_command" : "proactive");
        _supersedes = isCmd;
        _grants = (ctx.ActiveMethods ?? new List<ActiveMethodGrant>())
            .ToDictionary(g => $"{g.MethodId}|{g.Actor}", StringComparer.Ordinal);
        _caps = sdk.GetCachedCapabilitiesByDigest(ctx.CapabilityTableDigest)
            .ToDictionary(c => c.MethodId, StringComparer.Ordinal);
    }

    public QueueBuilder AddStep(
        string methodId,
        string actor,
        Dictionary<string, object?>? args = null,
        bool validate = true,
        bool allowInitialStaged = false)
    {
        var stepArgs = args != null ? new Dictionary<string, object?>(args) : new Dictionary<string, object?>();
        int idx = _steps.Count;
        if (validate)
        {
            var scope = _sdk.CurrentScope;
            if (scope?.PermittedMethods != null &&
                scope.PermittedMethods.TryGetValue(methodId, out var allowedActors) &&
                !allowedActors.Contains(actor))
            {
                throw new G2ACoreException("permission_denied", 403, $"Scope forbids ({methodId}, {actor})");
            }
            if (!_grants.TryGetValue($"{methodId}|{actor}", out var grant))
                throw new G2ACoreException("invalid_arguments", 400, $"Method ({methodId}, {actor}) not active in context");
            if (idx == 0 && !allowInitialStaged && grant.Readiness != "ready")
                throw new G2ACoreException("stale_context", 409, $"Initial step ({methodId}, {actor}) is staged");
            if (stepArgs.TryGetValue("target_id", out var tidObj) && tidObj is string tid && grant.Targets.Count > 0)
            {
                var cand = grant.Targets.FirstOrDefault(t => t.TargetId == tid)
                    ?? throw new G2ACoreException("not_found", 404, $"Target '{tid}' not in active candidates");
                if (idx == 0 && !allowInitialStaged && cand.Readiness != "ready")
                    throw new G2ACoreException("stale_context", 409, $"Initial target '{tid}' is staged");
            }
            if (_caps.TryGetValue(methodId, out var cap) && cap.ParamsSchema is { Count: > 0 })
            {
                if (!OpenAICompatibleLlmPlanner.ValidateObjectSchema(stepArgs, cap.ParamsSchema, 0))
                    throw new G2ACoreException("invalid_arguments", 400, $"Arguments for {methodId} failed local JSON Schema check");
            }
        }
        _steps.Add(new QueueStep(idx, methodId, actor, stepArgs));
        return this;
    }

    public QueueBuilder AddStepAuto(
        string methodId,
        string actor,
        Dictionary<string, object?>? args = null,
        bool validate = true)
    {
        var chain = OpenAICompatibleLlmPlanner.ResolvePrerequisiteSteps(methodId, actor, args, _ctx, _caps.Values.ToList());
        foreach (var st in chain)
            AddStep(st.MethodId, st.Actor, st.Args, validate);
        return this;
    }

    public MethodCallQueue Build(string? queueId = null)
    {
        if (_steps.Count == 0)
            throw new G2ACoreException("invalid_arguments", 400, "Cannot build an empty MethodCallQueue");
        return new MethodCallQueue(
            QueueId: queueId ?? ("Q-" + Guid.NewGuid().ToString("N")[..12]),
            Epoch: _ctx.Epoch,
            BasedOnRev: _ctx.Rev,
            Trigger: _trigger,
            Supersedes: _supersedes,
            IntentSummary: _intentSummary,
            Steps: _steps.ToList());
    }

    public Task<QueueReceipt?> SubmitAsync(string? queueId = null, CancellationToken ct = default) =>
        _sdk.SubmitQueueWithRecoveryAsync(Build(queueId), ct);
}
