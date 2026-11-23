using System.Text.Json.Serialization;

namespace G2A.InteractionOrigin;

public sealed class G2AOriginException : Exception
{
    public string Code { get; }
    public int HttpStatus { get; }

    public G2AOriginException(string code, int httpStatus, string message) : base(message)
    {
        Code = code;
        HttpStatus = httpStatus;
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
    [property: JsonPropertyName("prerequisite_hint"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? PrerequisiteHint = null,
    [property: JsonPropertyName("actor_anchor"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? ActorAnchor = null,
    [property: JsonPropertyName("player_anchor"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? PlayerAnchor = null,
    [property: JsonPropertyName("alert_tag"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? AlertTag = null
);

public sealed record ActiveMethodGrant(
    [property: JsonPropertyName("method_id")] string MethodId,
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("readiness")] string Readiness,
    [property: JsonPropertyName("targets")] List<TargetCandidate> Targets
);

public sealed record MethodActorKey(
    [property: JsonPropertyName("method_id")] string MethodId,
    [property: JsonPropertyName("actor")] string Actor
);

public sealed record OriginExecuteRequest(
    [property: JsonPropertyName("slot_id")] string SlotId,
    [property: JsonPropertyName("epoch")] long Epoch,
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("execution_id")] string ExecutionId,
    [property: JsonPropertyName("method_id")] string? MethodId,
    [property: JsonPropertyName("actor")] string? Actor,
    [property: JsonPropertyName("args")] Dictionary<string, object?> Args
);

public sealed record OriginExecuteReply(
    [property: JsonPropertyName("execution_id")] string ExecutionId,
    [property: JsonPropertyName("state")] string State,
    [property: JsonPropertyName("effect_summary"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] Dictionary<string, object?>? EffectSummary = null,
    [property: JsonPropertyName("failure_reason"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? FailureReason = null
);

public abstract record EffectInvocationResult
{
    public sealed record ImmediateSucceeded(Dictionary<string, object?> EffectSummary) : EffectInvocationResult;
    public sealed record ImmediateFailed(string Reason) : EffectInvocationResult;
    public sealed record StartedExecuting(Action CancelFn) : EffectInvocationResult;
}

public interface IEngineDispatcherPort
{
    T RunOnGameThread<T>(Func<T> task);
}

public sealed class LockingDispatcher : IEngineDispatcherPort
{
    private readonly object _gate = new();
    public T RunOnGameThread<T>(Func<T> task) { lock (_gate) { return task(); } }
}

public interface IHubTransportClientPort
{
    void PostToHub(string operation, object payload);
}
