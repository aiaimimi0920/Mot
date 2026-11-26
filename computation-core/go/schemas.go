package g2acore

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

const G2AWireVersion = "0.0.1"

type G2AError struct {
	Code         string            `json:"code"`
	HTTPStatus   int               `json:"http_status"`
	Message      string            `json:"message"`
	RetryAfterMs *int64            `json:"retry_after_ms,omitempty"`
	CooldownMs   *int64            `json:"cooldown_ms,omitempty"`
	FreshContext *ProjectedContext `json:"fresh_context,omitempty"`
}

func (e *G2AError) Error() string {
	return fmt.Sprintf("%s (%d): %s", e.Code, e.HTTPStatus, e.Message)
}

type CapabilityEntry struct {
	MethodID        string         `json:"method_id"`
	Name            string         `json:"name"`
	Description     string         `json:"description"`
	PermittedActors []string       `json:"permitted_actors"`
	ParamsSchema    map[string]any `json:"params_schema"`
	Sustained       bool           `json:"sustained"`
	RiskFlags       []string       `json:"risk_flags"`
	DefaultPolicy   string         `json:"default_policy"`
}

type TargetCandidate struct {
	TargetID         string  `json:"target_id"`
	Label            string  `json:"label"`
	Readiness        string  `json:"readiness"`
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

type ProjectedContext struct {
	SlotID                string                    `json:"slot_id"`
	Epoch                 int64                     `json:"epoch"`
	Rev                   int64                     `json:"rev"`
	CapabilityTableDigest string                    `json:"capability_table_digest"`
	PlayerCommand         *string                   `json:"player_command,omitempty"`
	StateBrief            map[string]any            `json:"state_brief"`
	ActiveMethods         []ActiveMethodGrant       `json:"active_methods"`
	VisualRef             map[string]any            `json:"visual_ref,omitempty"`
	AudioRef              map[string]any            `json:"audio_ref,omitempty"`
	AllowedTimelineTags   []string                  `json:"allowed_timeline_tags,omitempty"`
	ExtensionsData        map[string]map[string]any `json:"extensions_data,omitempty"`
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
	ExecutionID   string         `json:"execution_id"`
	State         string         `json:"state"`
	EffectSummary map[string]any `json:"effect_summary,omitempty"`
	FailureReason string         `json:"failure_reason,omitempty"`
}

type QueueReceipt struct {
	QueueID      string        `json:"queue_id"`
	QueueState   string        `json:"queue_state"`
	StepReceipts []StepReceipt `json:"step_receipts"`
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
}

type PlanDecision struct {
	IntentSummary string
	PlannedSteps  []QueueStep
	ReplyText     string
	Audience      string
	GoalStatus    string // "completed" | "continue" | "blocked"
	MemoryNote    string
}

type GoalExecutionReport struct {
	GoalID          string            `json:"goal_id"`
	Objective       string            `json:"objective"`
	Succeeded       bool              `json:"succeeded"`
	Status          string            `json:"status"`
	TurnsUsed       int               `json:"turns_used"`
	SubmittedQueues []string          `json:"submitted_queues"`
	FinalContext    *ProjectedContext `json:"final_context,omitempty"`
	FailureReason   string            `json:"failure_reason,omitempty"`
}

type CognitivePlanner interface {
	Plan(capabilities []CapabilityEntry, ctx ProjectedContext, recalledMemories []MemoryRecord) (PlanDecision, error)
}

type NoOpPlanner struct{}

func (NoOpPlanner) Plan(_ []CapabilityEntry, _ ProjectedContext, _ []MemoryRecord) (PlanDecision, error) {
	return PlanDecision{}, nil
}

// DiscoverLocalHubCoreURL reads `core_url` from the local Intelligent Hub runtime manifest (`hub.runtime.json`).
func DiscoverLocalHubCoreURL(explicitRuntimeFile string) (string, error) {
	path := explicitRuntimeFile
	if path == "" {
		path = os.Getenv("G2A_HUB_RUNTIME_FILE")
	}
	if path == "" {
		if runtime.GOOS == "windows" {
			if localApp := os.Getenv("LOCALAPPDATA"); localApp != "" {
				path = filepath.Join(localApp, "G2A", "hub.runtime.json")
			}
		}
		if path == "" {
			home, _ := os.UserHomeDir()
			path = filepath.Join(home, ".g2a", "hub.runtime.json")
		}
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", &G2AError{Code: "unavailable", HTTPStatus: 503, Message: fmt.Sprintf("Local Hub runtime manifest not found at %s", path)}
	}
	var doc map[string]any
	if err := UnmarshalStrict(raw, &doc); err != nil {
		return "", &G2AError{Code: "invalid_request", HTTPStatus: 400, Message: err.Error()}
	}
	coreURL, _ := doc["core_url"].(string)
	if strings.TrimSpace(coreURL) == "" {
		return "", &G2AError{Code: "unavailable", HTTPStatus: 503, Message: "Missing core_url in runtime manifest"}
	}
	return strings.TrimRight(coreURL, "/"), nil
}
