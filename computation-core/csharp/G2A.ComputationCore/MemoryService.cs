using System.Text.Json.Serialization;

namespace G2A.ComputationCore;

public sealed record MemoryRecord(
    [property: JsonPropertyName("memory_id")] string MemoryId,
    [property: JsonPropertyName("created_at_ms")] long CreatedAtMs,
    [property: JsonPropertyName("epistemic_source")] string EpistemicSource,
    [property: JsonPropertyName("pinned_by_player")] bool PinnedByPlayer,
    [property: JsonPropertyName("max_disclosure_scope")] string MaxDisclosureScope,
    [property: JsonPropertyName("content")] string Content,
    [property: JsonPropertyName("imported_at_ms")] long? ImportedAtMs = null,
    [property: JsonPropertyName("game_origin_id")] string? GameOriginId = null,
    [property: JsonPropertyName("timeline_tag")] string? TimelineTag = null,
    [property: JsonPropertyName("observed_epoch")] long? ObservedEpoch = null
);

public sealed record SpoilerGuard(
    long CurrentEpoch = 0,
    string GameOriginId = "",
    List<string>? AllowedTimelineTags = null,
    string QueryHint = "",
    int MaxResults = 48
);

public sealed record MemoryBundleV1(
    [property: JsonPropertyName("exported_at_ms")] long ExportedAtMs,
    [property: JsonPropertyName("persona_settings")] Dictionary<string, object?> PersonaSettings,
    [property: JsonPropertyName("autonomy_preferences")] Dictionary<string, string> AutonomyPreferences,
    [property: JsonPropertyName("records")] List<MemoryRecord> Records,
    [property: JsonPropertyName("format")] string Format = MemoryService.BundleFormatV1
);

public sealed record ImportReceiptV1(
    [property: JsonPropertyName("record_statuses")] Dictionary<string, string> RecordStatuses,
    [property: JsonPropertyName("applied_settings")] List<string> AppliedSettings,
    [property: JsonPropertyName("unsupported_settings")] List<string> UnsupportedSettings
);

public sealed class InMemoryCompanionStore
{
    private readonly object _gate = new();
    private readonly Dictionary<string, Dictionary<string, MemoryRecord>> _records = new();
    internal readonly Dictionary<string, Dictionary<string, object?>> Persona = new();
    internal readonly Dictionary<string, Dictionary<string, string>> Autonomy = new();

    public void UpsertPlayerRecord(string playerId, MemoryRecord record)
    {
        lock (_gate)
        {
            if (!_records.TryGetValue(playerId, out var map))
            {
                map = new Dictionary<string, MemoryRecord>();
                _records[playerId] = map;
            }
            map[record.MemoryId] = record;
        }
    }

    public List<MemoryRecord> ListRecords(string playerId)
    {
        lock (_gate)
        {
            if (!_records.TryGetValue(playerId, out var map)) return new List<MemoryRecord>();
            return map.Values.OrderByDescending(r => r.CreatedAtMs).ToList();
        }
    }

    public List<MemoryRecord> RecallSafe(string playerId, SpoilerGuard guard)
    {
        lock (_gate)
        {
            if (!_records.TryGetValue(playerId, out var map)) return new List<MemoryRecord>();
            var allowedTags = guard.AllowedTimelineTags is { Count: > 0 }
                ? new HashSet<string>(guard.AllowedTimelineTags, StringComparer.Ordinal)
                : null;
            var keywords = guard.QueryHint
                .Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Where(w => w.Length >= 2)
                .Select(w => w.ToLowerInvariant())
                .ToList();

            return map.Values
                .Where(r =>
                {
                    if (r.MaxDisclosureScope == "private_player_only") return false;
                    if (r.MaxDisclosureScope == "same_game_companion" &&
                        !string.IsNullOrEmpty(r.GameOriginId) &&
                        !string.IsNullOrEmpty(guard.GameOriginId) &&
                        r.GameOriginId != guard.GameOriginId) return false;
                    if (r.ObservedEpoch.HasValue && guard.CurrentEpoch > 0 && r.ObservedEpoch.Value > guard.CurrentEpoch)
                        return false;
                    if (allowedTags != null && !string.IsNullOrEmpty(r.TimelineTag) && !allowedTags.Contains(r.TimelineTag))
                        return false;
                    return true;
                })
                .Select(r =>
                {
                    int score = r.PinnedByPlayer ? 1000 : 0;
                    string low = r.Content.ToLowerInvariant();
                    foreach (var kw in keywords)
                    {
                        if (low.Contains(kw, StringComparison.Ordinal)) score += 10;
                    }
                    return (Record: r, Score: score);
                })
                .OrderByDescending(x => x.Score)
                .ThenByDescending(x => x.Record.CreatedAtMs)
                .Take(guard.MaxResults > 0 ? guard.MaxResults : 48)
                .Select(x => x.Record)
                .ToList();
        }
    }

    internal T WithLock<T>(Func<Dictionary<string, Dictionary<string, MemoryRecord>>, T> fn)
    {
        lock (_gate) { return fn(_records); }
    }
}

public sealed class MemoryService
{
    public const string BundleFormatV1 = "g2a.memory.bundle.v1";

    private static readonly Dictionary<string, int> ScopeRank = new(StringComparer.Ordinal)
    {
        ["private_player_only"] = 0,
        ["same_game_companion"] = 1,
        ["cross_game_companion"] = 2
    };

    private static readonly HashSet<string> ValidSources = new(StringComparer.Ordinal)
    {
        "game_observed", "player_stated", "agent_summary", "player_edited"
    };

    private static readonly HashSet<string> SupportedPersonaKeys = new(StringComparer.Ordinal)
    {
        "tone", "verbosity", "nickname", "language"
    };

    private readonly InMemoryCompanionStore _store;
    private readonly string _platformCeiling;

    public MemoryService(InMemoryCompanionStore store, string platformCeiling = "same_game_companion")
    {
        _store = store;
        _platformCeiling = platformCeiling;
    }

    public MemoryBundleV1 ExportBundle(string playerId) =>
        _store.WithLock(records =>
        {
            var p = _store.Persona.TryGetValue(playerId, out var pm) ? new Dictionary<string, object?>(pm) : new();
            var a = _store.Autonomy.TryGetValue(playerId, out var am) ? new Dictionary<string, string>(am) : new();
            var list = records.TryGetValue(playerId, out var rm)
                ? rm.Values.OrderBy(r => r.MemoryId, StringComparer.Ordinal).ToList()
                : new List<MemoryRecord>();
            return new MemoryBundleV1(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(), p, a, list);
        });

    public ImportReceiptV1 ImportBundle(string playerId, MemoryBundleV1 bundle, IEnumerable<string>? selectedIds = null)
    {
        if (bundle.Format != BundleFormatV1)
            throw new G2ACoreException("invalid_arguments", 400, $"Unsupported memory bundle format '{bundle.Format}'");

        var sel = selectedIds != null ? new HashSet<string>(selectedIds, StringComparer.Ordinal) : null;
        return _store.WithLock(records =>
        {
            if (!records.TryGetValue(playerId, out var playerRecs))
            {
                playerRecs = new Dictionary<string, MemoryRecord>();
                records[playerId] = playerRecs;
            }
            if (!_store.Persona.TryGetValue(playerId, out var personaMap))
            {
                personaMap = new Dictionary<string, object?>();
                _store.Persona[playerId] = personaMap;
            }
            if (!_store.Autonomy.TryGetValue(playerId, out var autoMap))
            {
                autoMap = new Dictionary<string, string>();
                _store.Autonomy[playerId] = autoMap;
            }

            var applied = new List<string>();
            var unsupported = new List<string>();
            foreach (var (k, v) in bundle.PersonaSettings)
            {
                if (SupportedPersonaKeys.Contains(k)) { personaMap[k] = v; applied.Add(k); }
                else unsupported.Add(k);
            }
            foreach (var (k, v) in bundle.AutonomyPreferences)
            {
                if (v is "confirm" or "prohibit") autoMap[k] = v;
                else unsupported.Add($"autonomy:{k}");
            }
            applied.Sort(StringComparer.Ordinal);
            unsupported.Sort(StringComparer.Ordinal);

            int ceilRank = ScopeRank[_platformCeiling];
            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            var statuses = new Dictionary<string, string>();

            foreach (var rec in bundle.Records)
            {
                if (sel is { Count: > 0 } && !sel.Contains(rec.MemoryId)) continue;
                if (string.IsNullOrWhiteSpace(rec.Content) || !ValidSources.Contains(rec.EpistemicSource) || !ScopeRank.TryGetValue(rec.MaxDisclosureScope, out int rRank))
                {
                    statuses[rec.MemoryId] = "rejected";
                    continue;
                }
                if (playerRecs.TryGetValue(rec.MemoryId, out var existing) && (existing.PinnedByPlayer || existing.EpistemicSource == "player_edited"))
                {
                    statuses[rec.MemoryId] = "rejected";
                    continue;
                }
                string finalScope = rRank > ceilRank ? _platformCeiling : rec.MaxDisclosureScope;
                playerRecs[rec.MemoryId] = rec with { MaxDisclosureScope = finalScope, ImportedAtMs = now };
                statuses[rec.MemoryId] = rRank > ceilRank ? "transformed" : "imported";
            }

            return new ImportReceiptV1(statuses, applied, unsupported);
        });
    }
}
