using System.Text.Json;

namespace G2A.InteractionOrigin;

public delegate EffectInvocationResult DomainEffectHandler(
    string actor,
    object subjectInstance,
    Dictionary<string, object?> args,
    string executionId
);

/// <summary>
/// Tier 1 Interaction Origin SDK for C# / Unity / .NET Game Hosts (G2A V0.0.1).
/// </summary>
public sealed class OriginSdk
{
    public const string WireVersion = "0.0.1";
    public const int MaxCandidateTargetsPerMethod = 24;
    public const int MaxExecutionLedgerEntries = 512;

    private readonly string _slotId;
    private readonly string _roleKind;
    private readonly string _billingMode;
    private readonly string _roomId;
    private readonly string _factionId;
    private readonly IHubTransportClientPort _transport;
    private readonly IEngineDispatcherPort _dispatcher;
    private readonly Action<string, string, bool>? _chatUiHandler;

    private readonly Dictionary<string, (CapabilityEntry Def, DomainEffectHandler Handler)> _capabilities = new();
    private readonly Dictionary<string, object> _actors = new();
    private readonly Dictionary<string, (Func<Dictionary<string, object?>> SnapshotFn, Func<List<ActiveMethodGrant>> GrantsFn)> _modules = new();
    private readonly Dictionary<string, Action> _inFlightSteps = new();
    private readonly Dictionary<string, OriginExecuteReply> _executionLedger = new();
    private readonly Queue<string> _ledgerOrder = new();
    private readonly List<OriginExecuteReply> _pendingSettled = new();

    private Dictionary<(string MethodId, string Actor), string> _lastSentMethodJson = new();
    private readonly Dictionary<string, Dictionary<string, object?>> _extensionsData = new();
    private Dictionary<string, object?>? _visualRef;
    private Dictionary<string, object?>? _audioRef;
    private List<string>? _allowedTimelineTags;
    private string _lastSentBriefDigest = "";
    private bool _syncEnabled = true;

    public long CurrentEpoch { get; private set; } = 1;
    public long CurrentRev { get; private set; } = 0;
    public long FramesSynced { get; private set; }
    public long ZeroChangeSkips { get; private set; }

    public OriginSdk(
        string slotId,
        IHubTransportClientPort transport,
        string roleKind = "companion",
        string billingMode = "player_metered",
        IEngineDispatcherPort? dispatcher = null,
        Action<string, string, bool>? chatUiHandler = null,
        string roomId = "",
        string factionId = "")
    {
        _slotId = slotId;
        _roleKind = roleKind;
        _billingMode = billingMode;
        _roomId = roomId;
        _factionId = factionId;
        _transport = transport;
        _dispatcher = dispatcher ?? new LockingDispatcher();
        _chatUiHandler = chatUiHandler;
    }

    public void RegisterCapability(CapabilityEntry def, DomainEffectHandler effectHandler)
    {
        var actors = _roleKind == "independent_npc"
            ? def.PermittedActors.Where(a => a != "player").ToList()
            : def.PermittedActors.ToList();
        var sanitized = def with { PermittedActors = actors, RiskFlags = def.RiskFlags ?? new List<string>() };
        _capabilities[sanitized.MethodId] = (sanitized, effectHandler);
    }

    public void BindActorSubject(string actor, object instance) => _actors[actor] = instance;

    public void BindStateModule(
        string moduleName,
        Func<Dictionary<string, object?>> snapshotFn,
        Func<List<ActiveMethodGrant>> activeMethodsFn)
    {
        _modules[moduleName] = (snapshotFn, activeMethodsFn);
    }

    public void NotifyModuleChanged(string moduleName, bool significantEvent = false)
    {
        if (_modules.ContainsKey(moduleName))
            FlushDirtySync(significantEvent);
    }

    public void AttachExtensionData(string extensionName, Dictionary<string, object?> data, bool flush = false)
    {
        _dispatcher.RunOnGameThread(() =>
        {
            _extensionsData[extensionName] = new Dictionary<string, object?>(data);
            return true;
        });
        if (flush) FlushDirtySync(false);
    }

    public void AttachVisualFrame(Dictionary<string, object?> visualRef, bool flush = false)
    {
        _dispatcher.RunOnGameThread(() =>
        {
            _visualRef = new Dictionary<string, object?>(visualRef);
            return true;
        });
        if (flush) FlushDirtySync(false);
    }

    public void AttachAudioSegment(Dictionary<string, object?> audioRef, bool flush = false)
    {
        _dispatcher.RunOnGameThread(() =>
        {
            _audioRef = new Dictionary<string, object?>(audioRef);
            return true;
        });
        if (flush) FlushDirtySync(false);
    }

    public void SetAllowedTimelineTags(IEnumerable<string> tags, bool flush = false)
    {
        _dispatcher.RunOnGameThread(() =>
        {
            _allowedTimelineTags = tags.ToList();
            return true;
        });
        if (flush) FlushDirtySync(true);
    }

    public void PublishCapabilitiesAndBaseline(
        string originExecuteUrl = "",
        bool disabled = false,
        int maxSessions = 0,
        long sharedCooldownMs = 0,
        List<string>? allowlistedCores = null,
        string allowlistCriteriaUrl = "")
    {
        var sortedDefs = _capabilities.Values
            .OrderBy(c => c.Def.MethodId, StringComparer.Ordinal)
            .Select(c => c.Def)
            .ToList();
        string capDigest = JcsDigest.ComputeSha256("g2a.cap.v1", sortedDefs);
        var capPayload = new Dictionary<string, object?>
        {
            ["slot_id"] = _slotId,
            ["role_kind"] = _roleKind,
            ["billing_mode"] = _billingMode,
            ["origin_execute_url"] = originExecuteUrl,
            ["capability_table_digest"] = capDigest,
            ["capabilities"] = sortedDefs
        };
        if (disabled) capPayload["disabled"] = true;
        if (maxSessions > 0) capPayload["max_sessions"] = maxSessions;
        if (sharedCooldownMs > 0) capPayload["shared_cooldown_ms"] = sharedCooldownMs;
        if (allowlistedCores is { Count: > 0 }) capPayload["allowlisted_cores"] = allowlistedCores;
        if (!string.IsNullOrEmpty(allowlistCriteriaUrl)) capPayload["allowlist_criteria_url"] = allowlistCriteriaUrl;
        if (!string.IsNullOrEmpty(_roomId)) capPayload["room_id"] = _roomId;
        if (!string.IsNullOrEmpty(_factionId)) capPayload["faction_id"] = _factionId;
        _transport.PostToHub("origin.capabilities", capPayload);
        FlushFullSync();
    }

    public bool FlushDirtySync(bool significantEvent = false)
    {
        if (!_syncEnabled && _pendingSettled.Count == 0) return false;

        var (brief, grants, settled, extSnapshot, visSnapshot, audSnapshot, tagsSnapshot) = _dispatcher.RunOnGameThread(() =>
        {
            var (b, g) = CollectCurrentView();
            var s = _pendingSettled.ToList();
            _pendingSettled.Clear();
            var ext = _extensionsData.ToDictionary(
                kv => kv.Key,
                kv => new Dictionary<string, object?>(kv.Value));
            var vis = _visualRef != null ? new Dictionary<string, object?>(_visualRef) : null;
            var aud = _audioRef != null ? new Dictionary<string, object?>(_audioRef) : null;
            var tags = _allowedTimelineTags?.ToList();
            return (b, g, s, ext, vis, aud, tags);
        });

        object digestTarget = (extSnapshot.Count > 0 || visSnapshot != null || audSnapshot != null || tagsSnapshot != null)
            ? new Dictionary<string, object?>
            {
                ["brief"] = brief,
                ["ext"] = extSnapshot.Count > 0 ? extSnapshot : null,
                ["visual"] = visSnapshot,
                ["audio"] = audSnapshot,
                ["timelines"] = tagsSnapshot
            }
            : brief;
        string briefDigest = JcsDigest.ComputeSha256("g2a.brief.v1", digestTarget);
        var currentSerialized = grants.ToDictionary(kv => kv.Key, kv => JsonSerializer.Serialize(kv.Value));

        var upserted = new List<ActiveMethodGrant>();
        var removed = new List<MethodActorKey>();

        foreach (var (key, json) in currentSerialized)
        {
            if (!_lastSentMethodJson.TryGetValue(key, out string? prevJson) || prevJson != json)
                upserted.Add(grants[key]);
        }
        foreach (var key in _lastSentMethodJson.Keys)
        {
            if (!currentSerialized.ContainsKey(key))
                removed.Add(new MethodActorKey(key.MethodId, key.Actor));
        }

        if (briefDigest == _lastSentBriefDigest && upserted.Count == 0 && removed.Count == 0 && settled.Count == 0)
        {
            ZeroChangeSkips++;
            return false;
        }

        long prevRev = CurrentRev;
        CurrentRev++;
        FramesSynced++;
        _lastSentBriefDigest = briefDigest;
        _lastSentMethodJson = currentSerialized;

        var frame = new Dictionary<string, object?>
        {
            ["slot_id"] = _slotId,
            ["epoch"] = CurrentEpoch,
            ["rev"] = CurrentRev,
            ["base_rev"] = prevRev,
            ["mode"] = "delta",
            ["significant_event"] = significantEvent,
            ["state_brief"] = brief,
            ["upserted_methods"] = upserted,
            ["removed_methods"] = removed,
            ["settled_executions"] = settled
        };
        if (visSnapshot != null) frame["visual_ref"] = visSnapshot;
        if (audSnapshot != null) frame["audio_ref"] = audSnapshot;
        if (tagsSnapshot != null) frame["allowed_timeline_tags"] = tagsSnapshot;
        if (extSnapshot.Count > 0) frame["extensions_data"] = extSnapshot;
        _transport.PostToHub("origin.sync", frame);
        return true;
    }

    public void FlushFullSync()
    {
        var (brief, grants, settled, extSnapshot, visSnapshot, audSnapshot, tagsSnapshot) = _dispatcher.RunOnGameThread(() =>
        {
            var (b, g) = CollectCurrentView();
            var s = _pendingSettled.ToList();
            _pendingSettled.Clear();
            var ext = _extensionsData.ToDictionary(
                kv => kv.Key,
                kv => new Dictionary<string, object?>(kv.Value));
            var vis = _visualRef != null ? new Dictionary<string, object?>(_visualRef) : null;
            var aud = _audioRef != null ? new Dictionary<string, object?>(_audioRef) : null;
            var tags = _allowedTimelineTags?.ToList();
            return (b, g, s, ext, vis, aud, tags);
        });

        CurrentRev++;
        FramesSynced++;
        object digestTarget = (extSnapshot.Count > 0 || visSnapshot != null || audSnapshot != null || tagsSnapshot != null)
            ? new Dictionary<string, object?>
            {
                ["brief"] = brief,
                ["ext"] = extSnapshot.Count > 0 ? extSnapshot : null,
                ["visual"] = visSnapshot,
                ["audio"] = audSnapshot,
                ["timelines"] = tagsSnapshot
            }
            : brief;
        _lastSentBriefDigest = JcsDigest.ComputeSha256("g2a.brief.v1", digestTarget);
        _lastSentMethodJson = grants.ToDictionary(kv => kv.Key, kv => JsonSerializer.Serialize(kv.Value));

        var frame = new Dictionary<string, object?>
        {
            ["slot_id"] = _slotId,
            ["epoch"] = CurrentEpoch,
            ["rev"] = CurrentRev,
            ["mode"] = "full",
            ["significant_event"] = true,
            ["state_brief"] = brief,
            ["upserted_methods"] = grants.Values.ToList(),
            ["removed_methods"] = new List<MethodActorKey>(),
            ["settled_executions"] = settled
        };
        if (visSnapshot != null) frame["visual_ref"] = visSnapshot;
        if (audSnapshot != null) frame["audio_ref"] = audSnapshot;
        if (tagsSnapshot != null) frame["allowed_timeline_tags"] = tagsSnapshot;
        if (extSnapshot.Count > 0) frame["extensions_data"] = extSnapshot;
        _transport.PostToHub("origin.sync", frame);
    }

    public OriginExecuteReply HandleOriginExecute(OriginExecuteRequest req)
    {
        if (req.Kind == "cancel")
            return CancelInFlightStep(req.ExecutionId);

        if (req.Kind == "deliver_chat")
        {
            return _dispatcher.RunOnGameThread(() =>
            {
                string aud = req.Args.TryGetValue("audience", out var a) ? a?.ToString() ?? "player_only" : "player_only";
                string txt = req.Args.TryGetValue("text", out var t) ? t?.ToString() ?? "" : "";
                _chatUiHandler?.Invoke(aud, txt, true);
                return new OriginExecuteReply(req.ExecutionId, "succeeded");
            });
        }

        bool shouldSync = false;
        var reply = _dispatcher.RunOnGameThread(() =>
        {
            if (_executionLedger.TryGetValue(req.ExecutionId, out var existing))
                return existing;
            if (req.Epoch != CurrentEpoch)
                throw new G2AOriginException("stale_context", 409, "Epoch mismatch");
            if (req.MethodId is null || !_capabilities.TryGetValue(req.MethodId, out var reg))
                throw new G2AOriginException("invalid_arguments", 400, "Unknown method_id");
            if (req.Actor is null || !reg.Def.PermittedActors.Contains(req.Actor))
                throw new G2AOriginException("permission_denied", 403, "Actor not permitted");
            if (!_actors.TryGetValue(req.Actor, out var subject))
                throw new G2AOriginException("not_found", 404, "Actor subject not present");

            if (reg.Def.ParamsSchema is not null)
                ValidateArgsAgainstSchema(req.Args, reg.Def.ParamsSchema, "", 0);

            var outcome = reg.Handler(req.Actor, subject, req.Args, req.ExecutionId);
            switch (outcome)
            {
                case EffectInvocationResult.ImmediateSucceeded ok:
                    var okRep = new OriginExecuteReply(req.ExecutionId, "succeeded", ok.EffectSummary);
                    StoreLedgerReply(okRep);
                    shouldSync = true;
                    return okRep;
                case EffectInvocationResult.ImmediateFailed fail:
                    var failRep = new OriginExecuteReply(req.ExecutionId, "failed", null, fail.Reason);
                    StoreLedgerReply(failRep);
                    shouldSync = true;
                    return failRep;
                case EffectInvocationResult.StartedExecuting exec:
                    _inFlightSteps[req.ExecutionId] = exec.CancelFn;
                    var execRep = new OriginExecuteReply(req.ExecutionId, "executing");
                    StoreLedgerReply(execRep);
                    return execRep;
                default:
                    throw new G2AOriginException("invalid_request", 400, "Unknown EffectInvocationResult");
            }
        });

        if (shouldSync) FlushDirtySync(false);
        return reply;
    }

    private void StoreLedgerReply(OriginExecuteReply rep)
    {
        if (!_executionLedger.ContainsKey(rep.ExecutionId))
            _ledgerOrder.Enqueue(rep.ExecutionId);
        _executionLedger[rep.ExecutionId] = rep;
        int guard = _ledgerOrder.Count;
        while (_executionLedger.Count > MaxExecutionLedgerEntries && guard-- > 0)
        {
            string oldest = _ledgerOrder.Dequeue();
            if (_inFlightSteps.ContainsKey(oldest))
                _ledgerOrder.Enqueue(oldest);
            else
                _executionLedger.Remove(oldest);
        }
    }

    public void CompleteStep(string executionId, bool succeeded, Dictionary<string, object?>? summary = null, string? reason = null)
    {
        CompleteStepsBatch(new[] { (executionId, succeeded, summary, reason) });
    }

    public int CompleteStepsBatch(
        IEnumerable<(string ExecutionId, bool Succeeded, Dictionary<string, object?>? Summary, string? Reason)> settlements)
    {
        int count = _dispatcher.RunOnGameThread(() =>
        {
            int n = 0;
            foreach (var (executionId, succeeded, summary, reason) in settlements)
            {
                if (!_inFlightSteps.Remove(executionId)) continue;
                var rep = new OriginExecuteReply(executionId, succeeded ? "succeeded" : "failed", summary, reason);
                StoreLedgerReply(rep);
                _pendingSettled.Add(rep);
                n++;
            }
            return n;
        });
        if (count > 0) FlushDirtySync(false);
        return count;
    }

    public OriginExecuteReply CancelInFlightStep(string executionId)
    {
        bool queued = false;
        var rep = _dispatcher.RunOnGameThread(() =>
        {
            if (_inFlightSteps.Remove(executionId, out var cancelFn))
            {
                cancelFn();
                var cancelled = new OriginExecuteReply(executionId, "cancelled", null, "cancelled_by_hub");
                StoreLedgerReply(cancelled);
                _pendingSettled.Add(cancelled);
                queued = true;
                return cancelled;
            }
            return _executionLedger.TryGetValue(executionId, out var prev)
                ? prev
                : new OriginExecuteReply(executionId, "cancelled");
        });
        if (queued) FlushDirtySync(false);
        return rep;
    }

    public void ReportPlayerPresence(bool online, int remainingOnlineHumans = 0)
    {
        _syncEnabled = online || remainingOnlineHumans > 0;
        _transport.PostToHub("origin.player_event", new Dictionary<string, object?>
        {
            ["slot_id"] = _slotId,
            ["kind"] = "presence_changed",
            ["online"] = online,
            ["remaining_online_humans"] = remainingOnlineHumans
        });
    }

    public void ReportPlayerCommand(string commandText)
    {
        FlushDirtySync(true);
        _transport.PostToHub("origin.player_event", new Dictionary<string, object?>
        {
            ["slot_id"] = _slotId,
            ["kind"] = "player_command",
            ["text"] = commandText
        });
    }

    public void ReportEmergencyStop()
    {
        var activeIds = _dispatcher.RunOnGameThread(() => _inFlightSteps.Keys.ToList());
        foreach (var execId in activeIds)
            CancelInFlightStep(execId);
        _transport.PostToHub("origin.player_event", new Dictionary<string, object?>
        {
            ["slot_id"] = _slotId,
            ["kind"] = "emergency_stop"
        });
    }

    public void AdvanceEpoch()
    {
        var activeIds = _dispatcher.RunOnGameThread(() =>
        {
            CurrentEpoch++;
            CurrentRev = 0;
            _lastSentMethodJson.Clear();
            return _inFlightSteps.Keys.ToList();
        });
        foreach (var execId in activeIds)
            CancelInFlightStep(execId);
        FlushFullSync();
    }

    public void ReportQueueApproval(string sessionId, string queueId, string queueDigest, string fingerprint, bool approved)
    {
        _transport.PostToHub("origin.player_event", new Dictionary<string, object?>
        {
            ["slot_id"] = _slotId,
            ["kind"] = "queue_approval",
            ["session_id"] = sessionId,
            ["queue_id"] = queueId,
            ["queue_digest"] = queueDigest,
            ["fingerprint"] = fingerprint,
            ["approved"] = approved
        });
    }

    public void ReportNpcHostHandoff(string newPlayerId)
    {
        string cleaned = newPlayerId.Trim();
        if (string.IsNullOrEmpty(cleaned))
            throw new G2AOriginException("invalid_arguments", 400, "new_player_id is required");
        if (_roleKind != "independent_npc" || _billingMode != "player_metered")
            throw new G2AOriginException("permission_denied", 403, "host_handoff requires player_metered independent_npc slot");

        var activeIds = _dispatcher.RunOnGameThread(() => _inFlightSteps.Keys.ToList());
        foreach (var execId in activeIds)
            CancelInFlightStep(execId);
        _syncEnabled = true;
        _transport.PostToHub("origin.player_event", new Dictionary<string, object?>
        {
            ["slot_id"] = _slotId,
            ["kind"] = "host_handoff",
            ["new_player_id"] = cleaned
        });
    }

    private (Dictionary<string, object?> Brief, Dictionary<(string MethodId, string Actor), ActiveMethodGrant> Grants) CollectCurrentView()
    {
        var brief = new Dictionary<string, object?>();
        var grants = new Dictionary<(string MethodId, string Actor), ActiveMethodGrant>();

        foreach (var (modName, mod) in _modules)
        {
            brief[modName] = mod.SnapshotFn();
            foreach (var g in mod.GrantsFn())
                MergeGrant(grants, g);
        }
        return (brief, grants);
    }

    private static double ParseAnchorMeters(string? anchor)
    {
        if (string.IsNullOrWhiteSpace(anchor)) return 1e9;
        string s = anchor.Trim();
        if (!s.EndsWith('m')) return 1e9;
        string core = s[..^1];
        int sp = core.LastIndexOf(' ');
        if (sp >= 0) core = core[(sp + 1)..];
        return double.TryParse(core, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out double d) ? d : 1e9;
    }

    private static List<TargetCandidate> PrioritizeTargets(IEnumerable<TargetCandidate> items) =>
        items
            .GroupBy(t => t.TargetId)
            .Select(grp => grp.FirstOrDefault(x => x.Readiness == "ready") ?? grp.First())
            .OrderBy(t => string.IsNullOrEmpty(t.AlertTag) ? 1 : 0)
            .ThenBy(t => t.PlayerAnchor != null && t.PlayerAnchor.Contains("pointed", StringComparison.OrdinalIgnoreCase) ? 0 : 1)
            .ThenBy(t => t.Readiness == "ready" ? 0 : 1)
            .ThenBy(t => ParseAnchorMeters(t.ActorAnchor))
            .ThenBy(t => t.TargetId, StringComparer.Ordinal)
            .Take(MaxCandidateTargetsPerMethod)
            .ToList();

    private static void MergeGrant(Dictionary<(string MethodId, string Actor), ActiveMethodGrant> grants, ActiveMethodGrant incoming)
    {
        var key = (incoming.MethodId, incoming.Actor);
        if (grants.TryGetValue(key, out var existing))
        {
            var mergedTargets = PrioritizeTargets(existing.Targets.Concat(incoming.Targets));
            string overall = (existing.Readiness == "ready" || incoming.Readiness == "ready" || mergedTargets.Any(t => t.Readiness == "ready"))
                ? "ready" : "staged";
            grants[key] = new ActiveMethodGrant(incoming.MethodId, incoming.Actor, overall, mergedTargets);
        }
        else
        {
            grants[key] = incoming with { Targets = PrioritizeTargets(incoming.Targets) };
        }
    }

    public static void ValidateArgsAgainstSchema(
        Dictionary<string, object?> args,
        Dictionary<string, object?> schema,
        string prefix = "",
        int depth = 0)
    {
        if (depth > 16)
            throw new G2AOriginException("invalid_arguments", 400, "Schema nesting depth exceeded");
        if (schema.Count == 0) return;

        if (schema.TryGetValue("required", out var reqObj) && reqObj is System.Collections.IEnumerable reqEnum and not string)
        {
            foreach (var item in reqEnum)
            {
                string? reqKey = item?.ToString();
                if (!string.IsNullOrEmpty(reqKey) && !args.ContainsKey(reqKey))
                {
                    string qual = string.IsNullOrEmpty(prefix) ? reqKey : $"{prefix}.{reqKey}";
                    throw new G2AOriginException("invalid_arguments", 400, $"Missing required argument '{qual}'");
                }
            }
        }

        if (schema.TryGetValue("properties", out var propsObj) && propsObj is Dictionary<string, object?> props)
        {
            if (schema.TryGetValue("additionalProperties", out var apObj) && apObj is false)
            {
                foreach (string k in args.Keys)
                {
                    if (!props.ContainsKey(k))
                    {
                        string qual = string.IsNullOrEmpty(prefix) ? k : $"{prefix}.{k}";
                        throw new G2AOriginException("invalid_arguments", 400, $"Unexpected argument '{qual}'");
                    }
                }
            }
            foreach (var (k, v) in args)
            {
                if (props.TryGetValue(k, out var specObj) && specObj is Dictionary<string, object?> spec)
                {
                    string qual = string.IsNullOrEmpty(prefix) ? k : $"{prefix}.{k}";
                    ValidateFieldSchema(qual, v, spec, depth + 1);
                }
            }
        }
    }

    private static void ValidateFieldSchema(string name, object? val, Dictionary<string, object?> spec, int depth)
    {
        if (depth > 16)
            throw new G2AOriginException("invalid_arguments", 400, "Schema nesting depth exceeded");
        if (!spec.TryGetValue("type", out var tObj) || tObj is not string ptype) return;

        switch (ptype)
        {
            case "string":
                if (val is not string s)
                    throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' must be string");
                if (TryGetDouble(spec, "minLength", out double minL) && s.Length < minL)
                    throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' shorter than minLength");
                if (TryGetDouble(spec, "maxLength", out double maxL) && s.Length > maxL)
                    throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' exceeds maxLength");
                if (spec.TryGetValue("enum", out var enumObj) && enumObj is System.Collections.IEnumerable enumList and not string)
                {
                    bool matched = false;
                    foreach (var e in enumList)
                    {
                        if (string.Equals(e?.ToString(), s, StringComparison.Ordinal)) { matched = true; break; }
                    }
                    if (!matched)
                        throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' not in enum");
                }
                break;
            case "boolean":
                if (val is not bool)
                    throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' must be boolean");
                break;
            case "integer":
                if (!TryConvertDouble(val, out double iv) || Math.Truncate(iv) != iv)
                    throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' must be integer");
                CheckNumericBounds(name, iv, spec);
                break;
            case "number":
                if (!TryConvertDouble(val, out double nv) || !double.IsFinite(nv))
                    throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' must be finite number");
                CheckNumericBounds(name, nv, spec);
                break;
            case "array":
                if (val is not System.Collections.IEnumerable arrEnum || val is string || val is System.Collections.IDictionary)
                    throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' must be array");
                var items = arrEnum.Cast<object?>().ToList();
                if (TryGetDouble(spec, "minItems", out double minI) && items.Count < minI)
                    throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' below minItems");
                if (TryGetDouble(spec, "maxItems", out double maxI) && items.Count > maxI)
                    throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' exceeds maxItems");
                if (spec.TryGetValue("items", out var itemSpecObj) && itemSpecObj is Dictionary<string, object?> itemSpec)
                {
                    for (int i = 0; i < items.Count; i++)
                        ValidateFieldSchema($"{name}[{i}]", items[i], itemSpec, depth + 1);
                }
                break;
            case "object":
                if (val is not Dictionary<string, object?> subObj)
                    throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' must be object");
                ValidateArgsAgainstSchema(subObj, spec, name, depth + 1);
                break;
        }
    }

    private static bool TryConvertDouble(object? v, out double d)
    {
        switch (v)
        {
            case int i: d = i; return true;
            case long l: d = l; return true;
            case double db: d = db; return true;
            case float fl: d = fl; return true;
            default: d = 0; return false;
        }
    }

    private static bool TryGetDouble(Dictionary<string, object?> spec, string key, out double d)
    {
        if (spec.TryGetValue(key, out var raw)) return TryConvertDouble(raw, out d);
        d = 0;
        return false;
    }

    private static void CheckNumericBounds(string name, double val, Dictionary<string, object?> spec)
    {
        if (TryGetDouble(spec, "minimum", out double minV) && val < minV)
            throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' below minimum");
        if (TryGetDouble(spec, "maximum", out double maxV) && val > maxV)
            throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' exceeds maximum");
        if (TryGetDouble(spec, "exclusiveMinimum", out double exMin) && val <= exMin)
            throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' must exceed exclusiveMinimum");
        if (TryGetDouble(spec, "exclusiveMaximum", out double exMax) && val >= exMax)
            throw new G2AOriginException("invalid_arguments", 400, $"Argument '{name}' must be below exclusiveMaximum");
    }
}
