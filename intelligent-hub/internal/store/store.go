package store

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sync"

	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/model"
)

type SlotRecord struct {
	SlotID                string                           `json:"slot_id"`
	RoleKind              string                           `json:"role_kind"`
	BillingMode           string                           `json:"billing_mode"`
	PlayerID              string                           `json:"player_id"`
	GameInstanceID        string                           `json:"game_instance_id"`
	OriginExecuteURL      string                           `json:"origin_execute_url"`
	PlayerOnline          bool                             `json:"player_online"`
	Capabilities          []model.CapabilityEntry          `json:"capabilities"`
	CapabilityByID        map[string]model.CapabilityEntry `json:"capability_by_id"`
	CapabilityTableDigest string                           `json:"capability_table_digest"`
	AllowlistEnabled      bool                             `json:"allowlist_enabled"`
	AllowlistedCores      map[string]bool                  `json:"allowlisted_cores"`
}

type SlotView struct {
	SlotID              string                              `json:"slot_id"`
	Epoch               int64                               `json:"epoch"`
	Rev                 int64                               `json:"rev"`
	LastSignificantRev  int64                               `json:"last_significant_rev"`
	StateBrief          map[string]any                      `json:"state_brief"`
	Methods             map[string]model.ActiveMethodGrant  `json:"methods"` // key: method_id + "|" + actor
	SettledExecutions   map[string]model.OriginExecuteReply `json:"settled_executions"`
	LatestVisual        map[string]any                      `json:"latest_visual,omitempty"`
	LatestAudio         map[string]any                      `json:"latest_audio,omitempty"`
	AllowedTimelineTags []string                            `json:"allowed_timeline_tags,omitempty"`
}

func MethodKey(methodID, actor string) string {
	return methodID + "|" + actor
}

func PolicyKey(slotID, methodID, actor string) string {
	return slotID + "|" + methodID + "|" + actor
}

type SessionRecord struct {
	SessionID              string            `json:"session_id"`
	CallerCoreID           string            `json:"caller_core_id"`
	Scope                  model.FrozenScope `json:"scope"`
	ScopeDigest            string            `json:"scope_digest"`
	State                  string            `json:"state"` // "active" | "disconnected_grace" | "paused_awaiting_player" | "closed"
	PlayerOnline           bool              `json:"player_online"`
	LocalFallbackActivated bool              `json:"local_fallback_activated"`
	LeaseDeadlineMs        int64             `json:"lease_deadline_ms"`
	IdleBackoffMs          int64             `json:"idle_backoff_ms"`
	NextAllowedPullMs      int64             `json:"next_allowed_pull_ms"`
	ActiveQueueID          *string           `json:"active_queue_id,omitempty"`
	ConsumptionUsed        int64             `json:"consumption_used"`
	ConsumptionCap         *int64            `json:"consumption_cap,omitempty"`
	NegotiatedExtensions   []string          `json:"negotiated_extensions,omitempty"`
	RateWindowStartMs      int64             `json:"rate_window_start_ms,omitempty"`
	RateWindowCount        int               `json:"rate_window_count,omitempty"`
}

type QueueLedgerRecord struct {
	SessionID           string                `json:"session_id"`
	QueueID             string                `json:"queue_id"`
	QueueDigest         string                `json:"queue_digest"`
	Fingerprint         string                `json:"fingerprint"`
	Queue               model.MethodCallQueue `json:"queue"`
	State               string                `json:"state"` // "awaiting_confirmation" | "running" | "completed" | "aborted" | "cancelled"
	RequiresRealMoneyUI bool                  `json:"requires_real_money_ui"`
	ApprovedDigest      *string               `json:"approved_digest,omitempty"`
	StepReceipts        []model.StepReceipt   `json:"step_receipts"`
	CurrentExecutionID  *string               `json:"current_execution_id,omitempty"`
}

func QueueKey(sessionID, queueID string) string {
	return sessionID + "|" + queueID
}

type AuditEntry struct {
	TimestampMs int64  `json:"timestamp_ms"`
	Category    string `json:"category"`
	SlotID      string `json:"slot_id,omitempty"`
	SessionID   string `json:"session_id,omitempty"`
	QueueID     string `json:"queue_id,omitempty"`
	Summary     string `json:"summary"`
}

type HubStoreData struct {
	Slots            map[string]*SlotRecord        `json:"slots"`
	SlotViews        map[string]*SlotView          `json:"slot_views"`
	Sessions         map[string]*SessionRecord     `json:"sessions"`
	Queues           map[string]*QueueLedgerRecord `json:"queues"`
	ChatDedup        map[string]bool               `json:"chat_dedup"`
	ConfirmCooldowns map[string]int64              `json:"confirm_cooldowns"` // session_id|fingerprint -> deadline_ms
	ApprovedScopes   map[string]bool               `json:"approved_scopes"`   // exact scope_digests approved for auto-join
	PolicyOverrides  map[string]string             `json:"policy_overrides"`  // slot_id|method_id|actor -> allow|confirm|prohibit
	AuditLog         []AuditEntry                  `json:"audit_log"`
}

func NewHubStoreData() *HubStoreData {
	return &HubStoreData{
		Slots:            make(map[string]*SlotRecord),
		SlotViews:        make(map[string]*SlotView),
		Sessions:         make(map[string]*SessionRecord),
		Queues:           make(map[string]*QueueLedgerRecord),
		ChatDedup:        make(map[string]bool),
		ConfirmCooldowns: make(map[string]int64),
		ApprovedScopes:   make(map[string]bool),
		PolicyOverrides:  make(map[string]string),
		AuditLog:         make([]AuditEntry, 0),
	}
}

func (d *HubStoreData) AppendAudit(nowMs int64, category, slotID, sessionID, queueID, summary string) {
	d.AuditLog = append(d.AuditLog, AuditEntry{
		TimestampMs: nowMs,
		Category:    category,
		SlotID:      slotID,
		SessionID:   sessionID,
		QueueID:     queueID,
		Summary:     summary,
	})
	if len(d.AuditLog) > 120 {
		d.AuditLog = d.AuditLog[len(d.AuditLog)-120:]
	}
}

func (d *HubStoreData) EnsureMaps() {
	if d.Slots == nil {
		d.Slots = make(map[string]*SlotRecord)
	}
	if d.SlotViews == nil {
		d.SlotViews = make(map[string]*SlotView)
	}
	if d.Sessions == nil {
		d.Sessions = make(map[string]*SessionRecord)
	}
	if d.Queues == nil {
		d.Queues = make(map[string]*QueueLedgerRecord)
	}
	if d.ChatDedup == nil {
		d.ChatDedup = make(map[string]bool)
	}
	if d.ConfirmCooldowns == nil {
		d.ConfirmCooldowns = make(map[string]int64)
	}
	if d.ApprovedScopes == nil {
		d.ApprovedScopes = make(map[string]bool)
	}
	if d.PolicyOverrides == nil {
		d.PolicyOverrides = make(map[string]string)
	}
}

func (d *HubStoreData) GetSessionForCaller(sessionID, callerCoreID string) (*SessionRecord, *model.G2AError) {
	sess, ok := d.Sessions[sessionID]
	if !ok {
		return nil, model.NewError(model.ErrNotFound, 404, "Unknown session_id")
	}
	if sess.CallerCoreID != callerCoreID {
		return nil, model.NewError(model.ErrPermissionDenied, 403, "Session belongs to a different Computation Core principal")
	}
	return sess, nil
}

func (d *HubStoreData) SessionsForSlot(slotID string) []*SessionRecord {
	out := make([]*SessionRecord, 0)
	for _, s := range d.Sessions {
		if s.Scope.SlotID == slotID {
			out = append(out, s)
		}
	}
	return out
}

func (d *HubStoreData) ActiveSessionsForSlot(slotID string) []*SessionRecord {
	out := make([]*SessionRecord, 0)
	for _, s := range d.Sessions {
		if s.Scope.SlotID == slotID && s.PlayerOnline && s.State == "active" {
			out = append(out, s)
		}
	}
	return out
}

type PersistencePort struct {
	mu       sync.Mutex
	filePath string
	data     *HubStoreData
}

func NewPersistencePort(filePath string) (*PersistencePort, error) {
	p := &PersistencePort{
		filePath: filePath,
		data:     NewHubStoreData(),
	}
	if filePath != "" {
		if raw, err := os.ReadFile(filePath); err == nil && len(raw) > 0 {
			loaded := NewHubStoreData()
			if err := json.Unmarshal(raw, loaded); err != nil {
				return nil, fmt.Errorf("load hub ledger %s: %w", filePath, err)
			}
			loaded.EnsureMaps()
			p.data = loaded
		}
	}
	return p, nil
}

func Atomic[T any](p *PersistencePort, tx func(d *HubStoreData) (T, *model.G2AError)) (T, *model.G2AError) {
	p.mu.Lock()
	defer p.mu.Unlock()

	var zero T
	snapshotBytes, err := json.Marshal(p.data)
	if err != nil {
		return zero, model.NewError(model.ErrUnavailable, 503, "Failed to snapshot store")
	}

	res, gErr := tx(p.data)
	if gErr != nil {
		rolledBack := NewHubStoreData()
		_ = json.Unmarshal(snapshotBytes, rolledBack)
		rolledBack.EnsureMaps()
		p.data = rolledBack
		return zero, gErr
	}

	if p.filePath != "" {
		if err := p.persistLocked(); err != nil {
			rolledBack := NewHubStoreData()
			_ = json.Unmarshal(snapshotBytes, rolledBack)
			rolledBack.EnsureMaps()
			p.data = rolledBack
			return zero, model.NewError(model.ErrUnavailable, 503, "Failed to persist ledger")
		}
	}
	return res, nil
}

func (p *PersistencePort) persistLocked() error {
	if err := os.MkdirAll(filepath.Dir(p.filePath), 0o755); err != nil {
		return err
	}
	out, err := json.MarshalIndent(p.data, "", "  ")
	if err != nil {
		return err
	}
	tmp := p.filePath + ".tmp"
	if err := os.WriteFile(tmp, out, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, p.filePath)
}

func ValidateArgsAgainstSchema(args map[string]any, schema map[string]any) *model.G2AError {
	if len(schema) == 0 {
		return nil
	}
	if args == nil {
		args = map[string]any{}
	}
	if reqRaw, ok := schema["required"]; ok {
		if reqList, ok := toStringSlice(reqRaw); ok {
			for _, field := range reqList {
				if _, exists := args[field]; !exists {
					return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Missing required argument %q", field))
				}
			}
		}
	}
	props, _ := schema["properties"].(map[string]any)
	if addl, ok := schema["additionalProperties"].(bool); ok && !addl && props != nil {
		for k := range args {
			if _, declared := props[k]; !declared {
				return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Unexpected argument %q", k))
			}
		}
	}
	for k, v := range args {
		if props == nil {
			continue
		}
		propSchema, ok := props[k].(map[string]any)
		if !ok {
			continue
		}
		if err := validateField(k, v, propSchema); err != nil {
			return err
		}
	}
	return nil
}

func validateField(name string, val any, schema map[string]any) *model.G2AError {
	expectedType, _ := schema["type"].(string)
	switch expectedType {
	case "string":
		s, ok := val.(string)
		if !ok {
			return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q must be string", name))
		}
		if enums, ok := toStringSlice(schema["enum"]); ok {
			found := false
			for _, e := range enums {
				if e == s {
					found = true
					break
				}
			}
			if !found {
				return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q not in enum", name))
			}
		}
	case "integer":
		num, ok := toFloat(val)
		if !ok || math.Trunc(num) != num {
			return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q must be integer", name))
		}
		if err := checkNumericBounds(name, num, schema); err != nil {
			return err
		}
	case "number":
		num, ok := toFloat(val)
		if !ok {
			return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q must be number", name))
		}
		if err := checkNumericBounds(name, num, schema); err != nil {
			return err
		}
	case "boolean":
		if _, ok := val.(bool); !ok {
			return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q must be boolean", name))
		}
	case "array":
		if _, ok := val.([]any); !ok {
			return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q must be array", name))
		}
	case "object":
		if _, ok := val.(map[string]any); !ok {
			return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q must be object", name))
		}
	}
	return nil
}

func checkNumericBounds(name string, num float64, schema map[string]any) *model.G2AError {
	if minVal, ok := toFloat(schema["minimum"]); ok && num < minVal {
		return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q below minimum", name))
	}
	if maxVal, ok := toFloat(schema["maximum"]); ok && num > maxVal {
		return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q exceeds maximum", name))
	}
	return nil
}

func toFloat(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, !math.IsNaN(n) && !math.IsInf(n, 0)
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	case json.Number:
		f, err := n.Float64()
		return f, err == nil && !math.IsNaN(f) && !math.IsInf(f, 0)
	default:
		return 0, false
	}
}

func toStringSlice(v any) ([]string, bool) {
	switch s := v.(type) {
	case []string:
		return s, true
	case []any:
		out := make([]string, 0, len(s))
		for _, item := range s {
			str, ok := item.(string)
			if !ok {
				return nil, false
			}
			out = append(out, str)
		}
		return out, true
	default:
		return nil, false
	}
}
