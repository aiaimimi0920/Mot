using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace G2A.ComputationCore;

public sealed class G2ACoreException : Exception
{
    public string Code { get; }
    public int HttpStatus { get; }
    public long? RetryAfterMs { get; }
    public long? CooldownMs { get; }
    public ProjectedContext? FreshContext { get; }

    public G2ACoreException(
        string code,
        int httpStatus,
        string message,
        long? retryAfterMs = null,
        long? cooldownMs = null,
        ProjectedContext? freshContext = null)
        : base($"{code} ({httpStatus}): {message}")
    {
        Code = code;
        HttpStatus = httpStatus;
        RetryAfterMs = retryAfterMs;
        CooldownMs = cooldownMs;
        FreshContext = freshContext;
    }
}

public sealed record CapabilityEntry(
    [property: JsonPropertyName("method_id")] string MethodId,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("description")] string Description,
    [property: JsonPropertyName("permitted_actors")] List<string> PermittedActors,
    [property: JsonPropertyName("params_schema")] Dictionary<string, object?> ParamsSchema,
    [property: JsonPropertyName("sustained")] bool Sustained = false,
    [property: JsonPropertyName("risk_flags")] List<string>? RiskFlags = null,
    [property: JsonPropertyName("default_policy")] string DefaultPolicy = "allow"
);

public sealed record TargetCandidate(
    [property: JsonPropertyName("target_id")] string TargetId,
    [property: JsonPropertyName("label")] string Label,
    [property: JsonPropertyName("readiness")] string Readiness,
    [property: JsonPropertyName("prerequisite_hint")] string? PrerequisiteHint = null,
    [property: JsonPropertyName("actor_anchor")] string? ActorAnchor = null,
    [property: JsonPropertyName("player_anchor")] string? PlayerAnchor = null,
    [property: JsonPropertyName("alert_tag")] string? AlertTag = null
);

public sealed record ActiveMethodGrant(
    [property: JsonPropertyName("method_id")] string MethodId,
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("readiness")] string Readiness,
    [property: JsonPropertyName("targets")] List<TargetCandidate> Targets
);

public sealed record ProjectedContext(
    [property: JsonPropertyName("slot_id")] string SlotId,
    [property: JsonPropertyName("epoch")] long Epoch,
    [property: JsonPropertyName("rev")] long Rev,
    [property: JsonPropertyName("capability_table_digest")] string CapabilityTableDigest,
    [property: JsonPropertyName("player_command")] string? PlayerCommand = null,
    [property: JsonPropertyName("state_brief")] Dictionary<string, object?>? StateBrief = null,
    [property: JsonPropertyName("active_methods")] List<ActiveMethodGrant>? ActiveMethods = null,
    [property: JsonPropertyName("visual_ref")] Dictionary<string, object?>? VisualRef = null,
    [property: JsonPropertyName("audio_ref")] Dictionary<string, object?>? AudioRef = null,
    [property: JsonPropertyName("allowed_timeline_tags")] List<string>? AllowedTimelineTags = null,
    [property: JsonPropertyName("extensions_data")] Dictionary<string, Dictionary<string, object?>>? ExtensionsData = null
);

public sealed record QueueStep(
    [property: JsonPropertyName("step_index")] int StepIndex,
    [property: JsonPropertyName("method_id")] string MethodId,
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("args")] Dictionary<string, object?> Args
);

public sealed record MethodCallQueue(
    [property: JsonPropertyName("queue_id")] string QueueId,
    [property: JsonPropertyName("epoch")] long Epoch,
    [property: JsonPropertyName("based_on_rev")] long BasedOnRev,
    [property: JsonPropertyName("trigger")] string Trigger,
    [property: JsonPropertyName("supersedes")] bool Supersedes,
    [property: JsonPropertyName("intent_summary")] string IntentSummary,
    [property: JsonPropertyName("steps")] List<QueueStep> Steps
);

public sealed record StepReceipt(
    [property: JsonPropertyName("step_index")] int StepIndex,
    [property: JsonPropertyName("execution_id")] string ExecutionId,
    [property: JsonPropertyName("state")] string State,
    [property: JsonPropertyName("effect_summary")] Dictionary<string, object?>? EffectSummary = null,
    [property: JsonPropertyName("failure_reason")] string? FailureReason = null
);

public sealed record QueueReceipt(
    [property: JsonPropertyName("queue_id")] string QueueId,
    [property: JsonPropertyName("queue_state")] string QueueState,
    [property: JsonPropertyName("step_receipts")] List<StepReceipt> StepReceipts
);

public sealed record FrozenScope(
    [property: JsonPropertyName("player_id")] string PlayerId,
    [property: JsonPropertyName("core_id")] string CoreId,
    [property: JsonPropertyName("game_instance_id")] string GameInstanceId,
    [property: JsonPropertyName("slot_id")] string SlotId,
    [property: JsonPropertyName("role_kind")] string RoleKind,
    [property: JsonPropertyName("billing_mode")] string BillingMode,
    [property: JsonPropertyName("permitted_methods")] Dictionary<string, List<string>> PermittedMethods,
    [property: JsonPropertyName("capability_table_digest")] string CapabilityTableDigest
);

public sealed record PlanDecision(
    string IntentSummary = "",
    List<QueueStep>? PlannedSteps = null,
    string? ReplyText = null,
    string Audience = "player_only"
);

public interface ICognitivePlanner
{
    Task<PlanDecision> PlanAsync(
        IReadOnlyList<CapabilityEntry> capabilities,
        ProjectedContext context,
        IReadOnlyList<MemoryRecord> recalledMemories,
        CancellationToken ct = default);
}

public sealed class NoOpPlanner : ICognitivePlanner
{
    public Task<PlanDecision> PlanAsync(
        IReadOnlyList<CapabilityEntry> capabilities,
        ProjectedContext context,
        IReadOnlyList<MemoryRecord> recalledMemories,
        CancellationToken ct = default) => Task.FromResult(new PlanDecision());
}

public static class HubDiscovery
{
    public static string DiscoverLocalHubCoreUrl(string? runtimeFilePath = null)
    {
        string path = runtimeFilePath ?? Environment.GetEnvironmentVariable("G2A_HUB_RUNTIME_FILE") ?? "";
        if (string.IsNullOrEmpty(path))
        {
            if (OperatingSystem.IsWindows())
            {
                string localApp = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                if (!string.IsNullOrEmpty(localApp))
                    path = Path.Combine(localApp, "G2A", "hub.runtime.json");
            }
            if (string.IsNullOrEmpty(path))
            {
                string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                path = Path.Combine(home, ".g2a", "hub.runtime.json");
            }
        }
        if (!File.Exists(path))
            throw new G2ACoreException("unavailable", 503, $"Local Hub runtime manifest not found at {path}");
        using var doc = JcsDigest.ParseStrictDocument(File.ReadAllText(path, Encoding.UTF8));
        string? coreUrl = doc.RootElement.TryGetProperty("core_url", out var el) ? el.GetString() : null;
        if (string.IsNullOrWhiteSpace(coreUrl))
            throw new G2ACoreException("unavailable", 503, "Missing core_url in runtime manifest");
        return coreUrl.TrimEnd('/');
    }
}
