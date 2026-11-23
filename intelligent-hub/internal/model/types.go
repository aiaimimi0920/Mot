package model

import "encoding/json"

const WireVersion = "0.0.1"

type AcceptanceState string

const (
	AcceptanceAccepted    AcceptanceState = "accepted"
	AcceptanceNotAccepted AcceptanceState = "not_accepted"
	AcceptanceUnknown     AcceptanceState = "unknown"
)

type ErrorCode string

const (
	ErrInvalidRequest        ErrorCode = "invalid_request"        // 400
	ErrInvalidArguments      ErrorCode = "invalid_arguments"      // 400
	ErrUnsupportedVersion    ErrorCode = "unsupported_version"    // 400
	ErrUnsupportedCapability ErrorCode = "unsupported_capability" // 400
	ErrUnauthenticated       ErrorCode = "unauthenticated"        // 401
	ErrPermissionDenied      ErrorCode = "permission_denied"      // 403
	ErrRevoked               ErrorCode = "revoked"                // 403
	ErrNotFound              ErrorCode = "not_found"              // 404
	ErrStaleContext          ErrorCode = "stale_context"          // 409
	ErrConflict              ErrorCode = "conflict"               // 409
	ErrConfirmationRequired  ErrorCode = "confirmation_required"  // 409
	ErrResourceLimit         ErrorCode = "resource_limit"         // 429
	ErrUnavailable           ErrorCode = "unavailable"            // 503
	ErrOutcomeUnknown        ErrorCode = "outcome_unknown"        // 504
)

type G2AError struct {
	Code         ErrorCode         `json:"code"`
	HTTPStatus   int               `json:"http_status"`
	Message      string            `json:"message"`
	RetryAfterMs *int64            `json:"retry_after_ms,omitempty"`
	CooldownMs   *int64            `json:"cooldown_ms,omitempty"`
	FreshContext *ProjectedContext `json:"fresh_context,omitempty"`
}

func (e *G2AError) Error() string {
	return string(e.Code) + ": " + e.Message
}

func NewError(code ErrorCode, status int, msg string) *G2AError {
	return &G2AError{Code: code, HTTPStatus: status, Message: msg}
}

type RequestEnvelope struct {
	G2AVersion  string          `json:"g2a_version"`
	RequestID   string          `json:"request_id"`
	SessionID   *string         `json:"session_id,omitempty"`
	TimestampMs int64           `json:"timestamp_ms"`
	Operation   string          `json:"operation"`
	Payload     json.RawMessage `json:"payload"`
	Extensions  map[string]any  `json:"extensions,omitempty"`
}

type ReplyEnvelope struct {
	G2AVersion  string          `json:"g2a_version"`
	RequestID   string          `json:"request_id"`
	SessionID   *string         `json:"session_id,omitempty"`
	TimestampMs int64           `json:"timestamp_ms"`
	Acceptance  AcceptanceState `json:"acceptance"`
	Result      any             `json:"result"`
	Error       *G2AError       `json:"error"`
}

type EventEnvelope struct {
	G2AVersion  string `json:"g2a_version"`
	EventID     string `json:"event_id"`
	SessionID   string `json:"session_id"`
	TimestampMs int64  `json:"timestamp_ms"`
	Event       string `json:"event"`
	Payload     any    `json:"payload"`
}

type CapabilityEntry struct {
	MethodID        string         `json:"method_id"`
	Name            string         `json:"name"`
	Description     string         `json:"description"`
	PermittedActors []string       `json:"permitted_actors"`
	ParamsSchema    map[string]any `json:"params_schema"`
	Sustained       bool           `json:"sustained"`
	RiskFlags       []string       `json:"risk_flags"`
	DefaultPolicy   string         `json:"default_policy"` // "allow" | "confirm" | "prohibit"
}

type TargetCandidate struct {
	TargetID         string  `json:"target_id"`
	Label            string  `json:"label"`
	Readiness        string  `json:"readiness"` // "ready" | "staged"
	PrerequisiteHint *string `json:"prerequisite_hint,omitempty"`
	ActorAnchor      *string `json:"actor_anchor,omitempty"`
	PlayerAnchor     *string `json:"player_anchor,omitempty"`
	AlertTag         *string `json:"alert_tag,omitempty"`
}

type ActiveMethodGrant struct {
	MethodID  string            `json:"method_id"`
	Actor     string            `json:"actor"`
	Readiness string            `json:"readiness"`
	Targets   []TargetCandidate `json:"targets"`
}

type MethodActorKey struct {
	MethodID string `json:"method_id"`
	Actor    string `json:"actor"`
}

type OriginExecuteRequest struct {
	SlotID      string         `json:"slot_id"`
	Epoch       int64          `json:"epoch"`
	Kind        string         `json:"kind"` // "invoke" | "cancel" | "deliver_chat"
	ExecutionID string         `json:"execution_id"`
	MethodID    *string        `json:"method_id,omitempty"`
	Actor       *string        `json:"actor,omitempty"`
	Args        map[string]any `json:"args"`
}

type OriginExecuteReply struct {
	ExecutionID   string         `json:"execution_id"`
	State         string         `json:"state"` // "executing" | "succeeded" | "failed" | "cancelled"
	EffectSummary map[string]any `json:"effect_summary,omitempty"`
	FailureReason *string        `json:"failure_reason,omitempty"`
}

type OriginCapabilitiesPayload struct {
	SlotID                string            `json:"slot_id"`
	RoleKind              string            `json:"role_kind"`    // "companion" | "independent_npc"
	BillingMode           string            `json:"billing_mode"` // "player_metered" | "developer_sponsored"
	CapabilityTableDigest string            `json:"capability_table_digest"`
	Capabilities          []CapabilityEntry `json:"capabilities"`
	OriginExecuteURL      string            `json:"origin_execute_url,omitempty"`
	GameInstanceID        string            `json:"game_instance_id,omitempty"`
	PlayerID              string            `json:"player_id,omitempty"`
	AllowlistedCores      []string          `json:"allowlisted_cores,omitempty"`
}

type OriginSyncFrame struct {
	SlotID              string               `json:"slot_id"`
	Epoch               int64                `json:"epoch"`
	Rev                 int64                `json:"rev"`
	BaseRev             *int64               `json:"base_rev,omitempty"`
	Mode                string               `json:"mode"` // "full" | "delta"
	SignificantEvent    bool                 `json:"significant_event"`
	StateBrief          map[string]any       `json:"state_brief"`
	UpsertedMethods     []ActiveMethodGrant  `json:"upserted_methods"`
	RemovedMethods      []MethodActorKey     `json:"removed_methods"`
	SettledExecutions   []OriginExecuteReply `json:"settled_executions"`
	VisualRef           map[string]any       `json:"visual_ref,omitempty"`
	AudioRef            map[string]any       `json:"audio_ref,omitempty"`
	AllowedTimelineTags []string             `json:"allowed_timeline_tags,omitempty"`
}

type OriginPlayerEvent struct {
	SlotID                string `json:"slot_id"`
	Kind                  string `json:"kind"` // "presence_changed" | "emergency_stop" | "player_command" | "queue_approval"
	Online                bool   `json:"online,omitempty"`
	RemainingOnlineHumans int    `json:"remaining_online_humans,omitempty"`
	Text                  string `json:"text,omitempty"`
	SessionID             string `json:"session_id,omitempty"`
	QueueID               string `json:"queue_id,omitempty"`
	QueueDigest           string `json:"queue_digest,omitempty"`
	Fingerprint           string `json:"fingerprint,omitempty"`
	Approved              bool   `json:"approved,omitempty"`
}

type ProjectedContext struct {
	SlotID                string              `json:"slot_id"`
	Epoch                 int64               `json:"epoch"`
	Rev                   int64               `json:"rev"`
	CapabilityTableDigest string              `json:"capability_table_digest"`
	PlayerCommand         *string             `json:"player_command,omitempty"`
	StateBrief            map[string]any      `json:"state_brief"`
	ActiveMethods         []ActiveMethodGrant `json:"active_methods"`
	VisualRef             map[string]any      `json:"visual_ref,omitempty"`
	AudioRef              map[string]any      `json:"audio_ref,omitempty"`
	AllowedTimelineTags   []string            `json:"allowed_timeline_tags,omitempty"`
}

type QueueStep struct {
	StepIndex int            `json:"step_index"`
	MethodID  string         `json:"method_id"`
	Actor     string         `json:"actor"`
	Args      map[string]any `json:"args"`
}

type MethodCallQueue struct {
	QueueID       string      `json:"queue_id"`
	Epoch         int64       `json:"epoch"`
	BasedOnRev    int64       `json:"based_on_rev"`
	Trigger       string      `json:"trigger"` // "player_command" | "proactive"
	Supersedes    bool        `json:"supersedes"`
	IntentSummary string      `json:"intent_summary"`
	Steps         []QueueStep `json:"steps"`
}

type StepReceipt struct {
	StepIndex     int            `json:"step_index"`
	MethodID      string         `json:"method_id"`
	Actor         string         `json:"actor"`
	State         string         `json:"state"` // "pending" | "executing" | "succeeded" | "failed" | "cancelled" | "aborted"
	EffectSummary map[string]any `json:"effect_summary,omitempty"`
	FailureReason *string        `json:"failure_reason,omitempty"`
}

type QueueReceipt struct {
	QueueID       string            `json:"queue_id"`
	QueueState    string            `json:"queue_state"` // "awaiting_confirmation" | "running" | "completed" | "aborted" | "cancelled"
	StepReceipts  []StepReceipt     `json:"step_receipts"`
	LatestContext *ProjectedContext `json:"latest_context,omitempty"`
}

type FrozenScope struct {
	PlayerID              string              `json:"player_id"`
	CoreID                string              `json:"core_id"`
	GameInstanceID        string              `json:"game_instance_id"`
	SlotID                string              `json:"slot_id"`
	RoleKind              string              `json:"role_kind"`
	BillingMode           string              `json:"billing_mode"`
	PermittedMethods      map[string][]string `json:"permitted_methods"`
	CapabilityTableDigest string              `json:"capability_table_digest"`
	AllowVisual           bool                `json:"allow_visual"`
	AllowAudio            bool                `json:"allow_audio"`
	AllowChat             bool                `json:"allow_chat"`
}

type RequestedScopeSpec struct {
	PermittedMethods map[string][]string `json:"permitted_methods,omitempty"`
	AllowVisual      bool                `json:"allow_visual"`
	AllowAudio       bool                `json:"allow_audio"`
	AllowChat        bool                `json:"allow_chat"`
}

type ExtensionSpec struct {
	Name     string `json:"name"`
	Required bool   `json:"required"`
}

type AuthorizeRequest struct {
	SlotID                 string             `json:"slot_id"`
	RequestedScope         RequestedScopeSpec `json:"requested_scope"`
	CachedCapabilityDigest *string            `json:"cached_capability_digest,omitempty"`
	ConsumptionCap         *int64             `json:"consumption_cap,omitempty"`
	Extensions             []ExtensionSpec    `json:"extensions,omitempty"`
}

type AuthorizeReply struct {
	SessionID             string            `json:"session_id"`
	ScopeDigest           string            `json:"scope_digest"`
	FrozenScope           FrozenScope       `json:"frozen_scope"`
	CapabilityTableDigest string            `json:"capability_table_digest"`
	CapabilityTable       []CapabilityEntry `json:"capability_table,omitempty"`
	NegotiatedExtensions  []string          `json:"negotiated_extensions,omitempty"`
}

type ResumeReply struct {
	ActionsPaused bool              `json:"actions_paused"`
	LatestContext *ProjectedContext `json:"latest_context,omitempty"`
}

type HeartbeatReply struct {
	LeaseDeadlineMs int64 `json:"lease_deadline_ms"`
}

type ChatMessage struct {
	MessageID  string `json:"message_id"`
	Audience   string `json:"audience"`
	Text       string `json:"text"`
	Provenance string `json:"provenance"`
}

type QueueIDPayload struct {
	QueueID string `json:"queue_id"`
}
