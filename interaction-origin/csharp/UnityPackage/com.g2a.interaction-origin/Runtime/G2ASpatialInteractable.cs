#nullable enable
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Events;

namespace G2A.InteractionOrigin.Unity
{
    /// <summary>
    /// Attach to chests, doors, switches, or NPCs in a Unity scene to expose them (or explicitly redact hidden secrets) on the G2A Origin Interface.
    /// </summary>
    public sealed class G2ASpatialInteractable : MonoBehaviour
    {
        private static readonly HashSet<G2ASpatialInteractable> _activeInstances = new();

        /// <summary>
        /// Zero-hierarchy-scan registry of enabled scene interactables, avoiding per-tick FindObjectsByType overhead.
        /// </summary>
        public static IReadOnlyCollection<G2ASpatialInteractable> ActiveInstances => _activeInstances;

        [Tooltip("Stable semantic identifier sent in active_methods (never expose raw memory addresses).")]
        public string targetId = "chest_01";

        [Tooltip("Human/AI-readable label describing the object.")]
        public string displayLabel = "Wooden Chest";

        [Tooltip("Distance in meters within which #02 interact becomes 'ready' instead of 'staged'.")]
        public float interactRadius = 2.0f;

        [Tooltip("When true, this object is strictly excluded from G2A state_brief and active_methods (anti-spoiler & fog-of-war confidentiality).")]
        public bool isHiddenSecret = false;

        [Tooltip("Optional player anchor hint (e.g., pointed_right) when highlighted by player camera ray.")]
        public string currentPlayerAnchor = "";

        [Tooltip("Optional operational alert tag (e.g., halted_no_material) prioritized in dense scenes.")]
        public string alertTag = "";

        public bool isOpened = false;

        public UnityEvent<string>? onInteractedByActor;

        private void OnEnable() => _activeInstances.Add(this);

        private void OnDisable() => _activeInstances.Remove(this);

        public Dictionary<string, object?> TriggerInteract(string actor)
        {
            isOpened = true;
            onInteractedByActor?.Invoke(actor);
            return new Dictionary<string, object?>
            {
                ["target_id"] = targetId,
                ["opened"] = true,
                ["by_actor"] = actor
            };
        }
    }
}
