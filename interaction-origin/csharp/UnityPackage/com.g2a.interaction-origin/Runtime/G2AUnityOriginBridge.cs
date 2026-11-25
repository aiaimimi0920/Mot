#nullable enable
using System;
using System.Collections.Generic;
using UnityEngine;

namespace G2A.InteractionOrigin.Unity
{
    /// <summary>
    /// Drop-in Unity MonoBehaviour hosting a G2A V0.0.1 Interaction Origin slot using zero-inbound-port outbound streaming ("pull://origin.execute").
    /// </summary>
    public sealed class G2AUnityOriginBridge : MonoBehaviour
    {
        [Header("Slot & Hub Connection")]
        public string slotId = "slot-unity-spatial";
        public string roleKind = "companion";
        public string billingMode = "player_metered";
        public string roomId = "";
        public string factionId = "";
        public bool autoDiscoverLocalHub = true;
        public string fallbackHubOriginUrl = "http://127.0.0.1:19210";
        public string originToken = "";

        [Header("Scene Actors & Sensors")]
        public Transform? playerTransform;
        public Transform? companionTransform;
        public float perceptionRadius = 25.0f;
        public float companionMoveSpeed = 4.5f;
        public float dirtySyncIntervalSeconds = 0.2f;

        private G2AUnityMainThreadDispatcher? _dispatcher;
        private HttpHubTransportClient? _transport;
        private OutboundExecuteStreamClient? _streamClient;
        private OriginSdk? _sdk;

        private string? _activeMoveExecId;
        private Transform? _activeMoveTarget;
        private string _activeMoveTargetId = "";
        private float _syncTimer;

        public event Action<string, string>? OnCompanionChatDelivered;

        private void Start()
        {
            _dispatcher = new G2AUnityMainThreadDispatcher();
            string resolvedUrl = fallbackHubOriginUrl;
            if (autoDiscoverLocalHub)
            {
                try
                {
                    _transport = HttpHubTransportClient.FromLocalHubDiscovery(sharedToken: originToken);
                    using var doc = System.Text.Json.JsonDocument.Parse(
                        System.IO.File.ReadAllText(HttpHubTransportClient.GetDefaultRuntimeFilePath()));
                    resolvedUrl = doc.RootElement.GetProperty("origin_url").GetString() ?? fallbackHubOriginUrl;
                }
                catch
                {
                    _transport = new HttpHubTransportClient(fallbackHubOriginUrl, originToken);
                }
            }
            else
            {
                _transport = new HttpHubTransportClient(fallbackHubOriginUrl, originToken);
            }

            _sdk = new OriginSdk(
                slotId: slotId,
                transport: _transport,
                roleKind: roleKind,
                billingMode: billingMode,
                dispatcher: _dispatcher,
                chatUiHandler: (audience, text, _) => OnCompanionChatDelivered?.Invoke(audience, text),
                roomId: roomId,
                factionId: factionId
            );

            if (companionTransform != null) _sdk.BindActorSubject("companion", companionTransform);
            if (playerTransform != null) _sdk.BindActorSubject("player", playerTransform);

            RegisterSpatialCapabilities();
            _sdk.BindStateModule("spatial", BuildSpatialSnapshot, BuildSpatialGrants);

            _streamClient = new OutboundExecuteStreamClient(_sdk, resolvedUrl, slotId, originToken);
            _sdk.PublishCapabilitiesAndBaseline(OutboundExecuteStreamClient.PullSentinelUrl);
        }

        private void Update()
        {
            _dispatcher?.PumpMainThreadQueue();

            if (_activeMoveExecId != null && companionTransform != null && _activeMoveTarget != null && _sdk != null)
            {
                companionTransform.position = Vector3.MoveTowards(
                    companionTransform.position,
                    _activeMoveTarget.position,
                    companionMoveSpeed * Time.deltaTime
                );
                if (Vector3.Distance(companionTransform.position, _activeMoveTarget.position) <= 1.25f)
                {
                    string finishedId = _activeMoveExecId;
                    string tid = _activeMoveTargetId;
                    _activeMoveExecId = null;
                    _activeMoveTarget = null;
                    _sdk.CompleteStep(finishedId, true, new Dictionary<string, object?> { ["arrived_at"] = tid });
                }
            }

            _syncTimer += Time.deltaTime;
            if (_syncTimer >= dirtySyncIntervalSeconds)
            {
                _syncTimer = 0f;
                _sdk?.FlushDirtySync(false);
            }
        }

        public void SendPlayerCommand(string naturalLanguageCommand) => _sdk?.ReportPlayerCommand(naturalLanguageCommand);

        public void TriggerEmergencyStop() => _sdk?.ReportEmergencyStop();

        private void OnDestroy()
        {
            _sdk?.ReportPlayerPresence(false, 0);
            _streamClient?.Dispose();
            _transport?.Dispose();
        }

        private void RegisterSpatialCapabilities()
        {
            if (_sdk == null) return;
            var targetIdSchema = new Dictionary<string, object?>
            {
                ["type"] = "object",
                ["required"] = new[] { "target_id" },
                ["properties"] = new Dictionary<string, object?>
                {
                    ["target_id"] = new Dictionary<string, object?> { ["type"] = "string" }
                }
            };

            _sdk.RegisterCapability(
                new CapabilityEntry(
                    MethodId: "#01",
                    Name: "move_to",
                    Description: "Navigate companion toward a visible scene target.",
                    PermittedActors: new List<string> { "companion" },
                    ParamsSchema: targetIdSchema,
                    Sustained: true,
                    RiskFlags: new List<string>(),
                    DefaultPolicy: "allow"
                ),
                (actor, _, args, execId) =>
                {
                    string tid = args.TryGetValue("target_id", out var v) ? v?.ToString() ?? "" : "";
                    var prop = FindVisibleProp(tid);
                    if (prop == null)
                        return new EffectInvocationResult.ImmediateFailed($"Target '{tid}' not visible");
                    _activeMoveExecId = execId;
                    _activeMoveTarget = prop.transform;
                    _activeMoveTargetId = tid;
                    return new EffectInvocationResult.StartedExecuting(() =>
                    {
                        _activeMoveExecId = null;
                        _activeMoveTarget = null;
                    });
                }
            );

            _sdk.RegisterCapability(
                new CapabilityEntry(
                    MethodId: "#02",
                    Name: "interact",
                    Description: "Interact with an in-range visible prop without locking player input.",
                    PermittedActors: new List<string> { "companion" },
                    ParamsSchema: targetIdSchema,
                    Sustained: false,
                    RiskFlags: new List<string>(),
                    DefaultPolicy: "allow"
                ),
                (actor, _, args, _) =>
                {
                    string tid = args.TryGetValue("target_id", out var v) ? v?.ToString() ?? "" : "";
                    var prop = FindVisibleProp(tid);
                    if (prop == null || companionTransform == null)
                        return new EffectInvocationResult.ImmediateFailed($"Target '{tid}' unavailable");
                    float dist = Vector3.Distance(companionTransform.position, prop.transform.position);
                    if (dist > prop.interactRadius)
                        return new EffectInvocationResult.ImmediateFailed("out_of_range");
                    return new EffectInvocationResult.ImmediateSucceeded(prop.TriggerInteract(actor));
                }
            );
        }

        private G2ASpatialInteractable? FindVisibleProp(string targetId)
        {
            foreach (var p in G2ASpatialInteractable.ActiveInstances)
            {
                if (p != null && !p.isHiddenSecret && p.targetId == targetId) return p;
            }
            return null;
        }

        private Dictionary<string, object?> BuildSpatialSnapshot()
        {
            var visible = new List<Dictionary<string, object?>>();
            if (companionTransform != null)
            {
                foreach (var p in G2ASpatialInteractable.ActiveInstances)
                {
                    if (p == null || p.isHiddenSecret) continue;
                    float dist = Vector3.Distance(companionTransform.position, p.transform.position);
                    if (dist <= perceptionRadius)
                    {
                        visible.Add(new Dictionary<string, object?>
                        {
                            ["target_id"] = p.targetId,
                            ["label"] = p.displayLabel,
                            ["opened"] = p.isOpened,
                            ["distance_m"] = Math.Round(dist, 1)
                        });
                    }
                }
            }
            return new Dictionary<string, object?>
            {
                ["companion_moving"] = _activeMoveExecId != null,
                ["visible_props"] = visible
            };
        }

        private List<ActiveMethodGrant> BuildSpatialGrants()
        {
            var moveTargets = new List<TargetCandidate>();
            var interactTargets = new List<TargetCandidate>();
            if (companionTransform != null)
            {
                foreach (var p in G2ASpatialInteractable.ActiveInstances)
                {
                    if (p == null || p.isHiddenSecret || p.isOpened) continue;
                    float dist = Vector3.Distance(companionTransform.position, p.transform.position);
                    if (dist > perceptionRadius) continue;
                    string? pAnchor = string.IsNullOrEmpty(p.currentPlayerAnchor) ? null : p.currentPlayerAnchor;
                    string? alert = string.IsNullOrEmpty(p.alertTag) ? null : p.alertTag;
                    moveTargets.Add(new TargetCandidate(p.targetId, p.displayLabel, "ready", null, $"{dist:F1}m", pAnchor, alert));
                    bool inRange = dist <= p.interactRadius;
                    interactTargets.Add(new TargetCandidate(
                        p.targetId,
                        p.displayLabel,
                        inRange ? "ready" : "staged",
                        inRange ? null : "move_within_interact_range",
                        $"{dist:F1}m",
                        pAnchor,
                        alert
                    ));
                }
            }
            var grants = new List<ActiveMethodGrant>();
            if (moveTargets.Count > 0)
                grants.Add(new ActiveMethodGrant("#01", "companion", "ready", moveTargets));
            if (interactTargets.Count > 0)
            {
                string overall = interactTargets.Exists(t => t.Readiness == "ready") ? "ready" : "staged";
                grants.Add(new ActiveMethodGrant("#02", "companion", overall, interactTargets));
            }
            return grants;
        }
    }
}
