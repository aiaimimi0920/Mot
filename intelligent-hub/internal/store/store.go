package store

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/model"
)

const (
	MaxAuditEntries         = 120
	MaxChatDedupEntries     = 2048
	MaxSettledPerSlot       = 256
	MaxTerminalQueuesStored = 512
	MaxClosedSessionsStored = 512
)

type SlotRecord struct {
	SlotID                string                           `json:"slot_id"`
	RoleKind              string                           `json:"role_kind"`
	BillingMode           string                           `json:"billing_mode"`
	PlayerID              string                           `json:"player_id"`
	GameInstanceID        string                           `json:"game_instance_id"`
	RoomID                string                           `json:"room_id,omitempty"`
	FactionID             string                           `json:"faction_id,omitempty"`
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
	SettledOrder        []string                            `json:"settled_order,omitempty"`
	LatestVisual        map[string]any                      `json:"latest_visual,omitempty"`
	LatestAudio         map[string]any                      `json:"latest_audio,omitempty"`
	AllowedTimelineTags []string                            `json:"allowed_timeline_tags,omitempty"`
}

func (v *SlotView) RecordSettledExecution(rep model.OriginExecuteReply) {
	if v.SettledExecutions == nil {
		v.SettledExecutions = make(map[string]model.OriginExecuteReply)
	}
	if _, exists := v.SettledExecutions[rep.ExecutionID]; !exists {
		v.SettledOrder = append(v.SettledOrder, rep.ExecutionID)
	}
	v.SettledExecutions[rep.ExecutionID] = rep
	if len(v.SettledOrder) > MaxSettledPerSlot {
		evictCount := len(v.SettledOrder) - MaxSettledPerSlot
		for _, oldID := range v.SettledOrder[:evictCount] {
			delete(v.SettledExecutions, oldID)
		}
		v.SettledOrder = append([]string(nil), v.SettledOrder[evictCount:]...)
	}
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
	LastContextPushMs      int64             `json:"last_context_push_ms,omitempty"`
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
	ConfirmDeadlineMs   int64                 `json:"confirm_deadline_ms,omitempty"`
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
	SessionOrder     []string                      `json:"session_order,omitempty"`
	Queues           map[string]*QueueLedgerRecord `json:"queues"`
	QueueOrder       []string                      `json:"queue_order,omitempty"`
	ChatDedup        map[string]bool               `json:"chat_dedup"`
	ChatDedupOrder   []string                      `json:"chat_dedup_order,omitempty"`
	ConfirmCooldowns map[string]int64              `json:"confirm_cooldowns"` // session_id|fingerprint -> deadline_ms
	ApprovedScopes   map[string]bool               `json:"approved_scopes"`   // exact scope_digests approved for auto-join
	PolicyOverrides  map[string]string             `json:"policy_overrides"`  // slot_id|method_id|actor -> allow|confirm|prohibit
	AuditLog         []AuditEntry                  `json:"audit_log"`

	slotSessions map[string]map[string]struct{} `json:"-"` // in-memory O(1) slot_id -> session_ids index
}

func NewHubStoreData() *HubStoreData {
	return &HubStoreData{
		Slots:            make(map[string]*SlotRecord),
		SlotViews:        make(map[string]*SlotView),
		Sessions:         make(map[string]*SessionRecord),
		SessionOrder:     make([]string, 0),
		Queues:           make(map[string]*QueueLedgerRecord),
		QueueOrder:       make([]string, 0),
		ChatDedup:        make(map[string]bool),
		ChatDedupOrder:   make([]string, 0),
		ConfirmCooldowns: make(map[string]int64),
		ApprovedScopes:   make(map[string]bool),
		PolicyOverrides:  make(map[string]string),
		AuditLog:         make([]AuditEntry, 0),
		slotSessions:     make(map[string]map[string]struct{}),
	}
}

func (d *HubStoreData) indexSessionSlot(sess *SessionRecord) {
	if sess == nil || sess.Scope.SlotID == "" {
		return
	}
	if d.slotSessions == nil {
		d.slotSessions = make(map[string]map[string]struct{})
	}
	bucket := d.slotSessions[sess.Scope.SlotID]
	if bucket == nil {
		bucket = make(map[string]struct{})
		d.slotSessions[sess.Scope.SlotID] = bucket
	}
	bucket[sess.SessionID] = struct{}{}
}

func (d *HubStoreData) unindexSessionSlot(slotID, sessionID string) {
	if d.slotSessions == nil {
		return
	}
	if bucket, ok := d.slotSessions[slotID]; ok {
		delete(bucket, sessionID)
		if len(bucket) == 0 {
			delete(d.slotSessions, slotID)
		}
	}
}

func (d *HubStoreData) RegisterSession(sess *SessionRecord) {
	if prev, exists := d.Sessions[sess.SessionID]; !exists {
		d.SessionOrder = append(d.SessionOrder, sess.SessionID)
	} else if prev.Scope.SlotID != sess.Scope.SlotID {
		d.unindexSessionSlot(prev.Scope.SlotID, sess.SessionID)
	}
	d.Sessions[sess.SessionID] = sess
	d.indexSessionSlot(sess)
	d.PruneClosedSessions()
}

// PruneClosedSessions bounds retained closed SessionRecords to MaxClosedSessionsStored so long-running
// Hubs never accumulate unbounded session history or slow down per-slot session scans.
func (d *HubStoreData) PruneClosedSessions() {
	if len(d.Sessions) <= MaxClosedSessionsStored {
		return
	}
	if len(d.SessionOrder) < len(d.Sessions) {
		seen := make(map[string]bool, len(d.SessionOrder))
		for _, sid := range d.SessionOrder {
			seen[sid] = true
		}
		for sid := range d.Sessions {
			if !seen[sid] {
				d.SessionOrder = append(d.SessionOrder, sid)
			}
		}
	}
	hasLiveQueue := make(map[string]bool)
	for _, rec := range d.Queues {
		if rec.State == "running" || rec.State == "awaiting_confirmation" {
			hasLiveQueue[rec.SessionID] = true
		}
	}
	closedCount := 0
	for _, s := range d.Sessions {
		if s.State == "closed" && !hasLiveQueue[s.SessionID] {
			closedCount++
		}
	}
	if closedCount <= MaxClosedSessionsStored {
		return
	}
	targetRemove := closedCount - MaxClosedSessionsStored
	removed := 0
	keptOrder := make([]string, 0, len(d.SessionOrder))
	for _, sid := range d.SessionOrder {
		sess, ok := d.Sessions[sid]
		if !ok {
			continue
		}
		if removed < targetRemove && sess.State == "closed" && !hasLiveQueue[sid] {
			d.unindexSessionSlot(sess.Scope.SlotID, sid)
			delete(d.Sessions, sid)
			removed++
			continue
		}
		keptOrder = append(keptOrder, sid)
	}
	d.SessionOrder = keptOrder
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
	if len(d.AuditLog) > MaxAuditEntries {
		d.AuditLog = append([]AuditEntry(nil), d.AuditLog[len(d.AuditLog)-MaxAuditEntries:]...)
	}
}

func (d *HubStoreData) RecordChatDedup(key string) bool {
	if d.ChatDedup[key] {
		return true
	}
	d.ChatDedup[key] = true
	d.ChatDedupOrder = append(d.ChatDedupOrder, key)
	if len(d.ChatDedupOrder) > MaxChatDedupEntries {
		evict := len(d.ChatDedupOrder) - MaxChatDedupEntries
		if evict < 128 {
			evict = 128
		}
		if evict > len(d.ChatDedupOrder) {
			evict = len(d.ChatDedupOrder)
		}
		for _, old := range d.ChatDedupOrder[:evict] {
			delete(d.ChatDedup, old)
		}
		d.ChatDedupOrder = append([]string(nil), d.ChatDedupOrder[evict:]...)
	}
	return false
}

func (d *HubStoreData) RegisterQueue(qKey string, rec *QueueLedgerRecord) {
	if _, exists := d.Queues[qKey]; !exists {
		d.QueueOrder = append(d.QueueOrder, qKey)
	}
	d.Queues[qKey] = rec
	d.PruneTerminalQueues()
}

func (d *HubStoreData) PruneTerminalQueues() {
	if len(d.Queues) <= MaxTerminalQueuesStored {
		return
	}
	keptOrder := make([]string, 0, len(d.QueueOrder))
	removed := 0
	targetRemove := len(d.Queues) - MaxTerminalQueuesStored
	for _, qKey := range d.QueueOrder {
		rec, ok := d.Queues[qKey]
		if !ok {
			continue
		}
		isTerminal := rec.State == "completed" || rec.State == "aborted" || rec.State == "cancelled"
		if removed < targetRemove && isTerminal {
			delete(d.Queues, qKey)
			removed++
			continue
		}
		keptOrder = append(keptOrder, qKey)
	}
	d.QueueOrder = keptOrder
}

func (d *HubStoreData) SweepExpiredCooldowns(nowMs int64) {
	for k, until := range d.ConfirmCooldowns {
		if nowMs >= until {
			delete(d.ConfirmCooldowns, k)
		}
	}
}

// SanitizeAfterRestart reconciles in-flight state after an unclean Hub process crash/restart:
//   - "active" sessions lose live push transports on restart -> transition to "disconnected_grace" (requiring two-tier resume);
//   - "running" queues had interrupted mid-execution steps -> transition to "aborted" so slots never deadlock.
func (d *HubStoreData) SanitizeAfterRestart() bool {
	mutated := false
	reason := "hub_restarted_mid_execution"
	for _, sess := range d.Sessions {
		if sess.State == "active" {
			sess.State = "disconnected_grace"
			sess.LocalFallbackActivated = true
			sess.ActiveQueueID = nil
			mutated = true
		}
	}
	for _, rec := range d.Queues {
		if rec.State == "running" {
			rec.State = "aborted"
			rec.CurrentExecutionID = nil
			for i := range rec.StepReceipts {
				if rec.StepReceipts[i].State == "pending" || rec.StepReceipts[i].State == "executing" {
					rec.StepReceipts[i].State = "aborted"
					rCopy := reason
					rec.StepReceipts[i].FailureReason = &rCopy
				}
			}
			mutated = true
		}
	}
	return mutated
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
	d.slotSessions = make(map[string]map[string]struct{})
	for _, s := range d.Sessions {
		d.indexSessionSlot(s)
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
	if d.slotSessions != nil {
		bucket := d.slotSessions[slotID]
		if len(bucket) == 0 {
			return nil
		}
		out := make([]*SessionRecord, 0, len(bucket))
		for sid := range bucket {
			if s, ok := d.Sessions[sid]; ok && s.Scope.SlotID == slotID {
				out = append(out, s)
			}
		}
		return out
	}
	out := make([]*SessionRecord, 0)
	for _, s := range d.Sessions {
		if s.Scope.SlotID == slotID {
			out = append(out, s)
		}
	}
	return out
}

func (d *HubStoreData) ActiveSessionsForSlot(slotID string) []*SessionRecord {
	if d.slotSessions != nil {
		bucket := d.slotSessions[slotID]
		if len(bucket) == 0 {
			return nil
		}
		out := make([]*SessionRecord, 0, len(bucket))
		for sid := range bucket {
			if s, ok := d.Sessions[sid]; ok && s.Scope.SlotID == slotID && s.PlayerOnline && s.State == "active" {
				out = append(out, s)
			}
		}
		return out
	}
	out := make([]*SessionRecord, 0)
	for _, s := range d.Sessions {
		if s.Scope.SlotID == slotID && s.PlayerOnline && s.State == "active" {
			out = append(out, s)
		}
	}
	return out
}

type PersistencePort struct {
	mu        sync.RWMutex
	filePath  string
	data      *HubStoreData
	dirty     bool
	stopFlush chan struct{}
	flushDone chan struct{}
	closeOnce sync.Once
}

func NewPersistencePort(filePath string) (*PersistencePort, error) {
	p := &PersistencePort{
		filePath:  filePath,
		data:      NewHubStoreData(),
		stopFlush: make(chan struct{}),
		flushDone: make(chan struct{}),
	}
	if filePath != "" {
		if raw, err := os.ReadFile(filePath); err == nil && len(raw) > 0 {
			loaded := NewHubStoreData()
			if err := json.Unmarshal(raw, loaded); err != nil {
				return nil, fmt.Errorf("load hub ledger %s: %w", filePath, err)
			}
			loaded.EnsureMaps()
			sanitized := loaded.SanitizeAfterRestart()
			p.data = loaded
			if sanitized {
				_ = p.persistLocked()
			}
		}
		go p.backgroundFlushLoop(250 * time.Millisecond)
	} else {
		close(p.flushDone)
	}
	return p, nil
}

func (p *PersistencePort) backgroundFlushLoop(interval time.Duration) {
	defer close(p.flushDone)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-p.stopFlush:
			_ = p.Flush()
			return
		case <-ticker.C:
			_ = p.Flush()
		}
	}
}

func (p *PersistencePort) Flush() error {
	if p.filePath == "" {
		return nil
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.dirty {
		return nil
	}
	if err := p.persistLocked(); err != nil {
		return err
	}
	p.dirty = false
	return nil
}

func (p *PersistencePort) Close() error {
	var err error
	p.closeOnce.Do(func() {
		if p.filePath != "" {
			close(p.stopFlush)
			<-p.flushDone
		}
	})
	return err
}

// ReadOnly executes a concurrent read transaction under RLock with zero JSON marshaling or disk I/O.
func ReadOnly[T any](p *PersistencePort, fn func(d *HubStoreData) (T, *model.G2AError)) (T, *model.G2AError) {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return fn(p.data)
}

// MutateVolatile executes a fast in-memory state update (e.g., 60Hz delta syncs, heartbeats, rate limits)
// without blocking on synchronous JSON snapshots or disk flushes; changes are coalesced by the background flusher.
// Callers MUST validate preconditions before mutating `d`.
func MutateVolatile[T any](p *PersistencePort, fn func(d *HubStoreData) (T, *model.G2AError)) (T, *model.G2AError) {
	p.mu.Lock()
	defer p.mu.Unlock()
	res, gErr := fn(p.data)
	if gErr == nil && p.filePath != "" {
		p.dirty = true
	}
	return res, gErr
}

func cloneOptionalString(p *string) *string {
	if p == nil {
		return nil
	}
	v := *p
	return &v
}

func cloneOptionalInt64(p *int64) *int64 {
	if p == nil {
		return nil
	}
	v := *p
	return &v
}

func cloneStringAnyMap(m map[string]any) map[string]any {
	if m == nil {
		return nil
	}
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

// Clone creates an in-memory structural copy of HubStoreData without reflection or JSON marshaling,
// enabling microsecond-scale rollback snapshots inside Atomic transactions.
func (d *HubStoreData) Clone() *HubStoreData {
	out := &HubStoreData{
		Slots:            make(map[string]*SlotRecord, len(d.Slots)),
		SlotViews:        make(map[string]*SlotView, len(d.SlotViews)),
		Sessions:         make(map[string]*SessionRecord, len(d.Sessions)),
		SessionOrder:     append([]string(nil), d.SessionOrder...),
		Queues:           make(map[string]*QueueLedgerRecord, len(d.Queues)),
		QueueOrder:       append([]string(nil), d.QueueOrder...),
		ChatDedup:        make(map[string]bool, len(d.ChatDedup)),
		ChatDedupOrder:   append([]string(nil), d.ChatDedupOrder...),
		ConfirmCooldowns: make(map[string]int64, len(d.ConfirmCooldowns)),
		ApprovedScopes:   make(map[string]bool, len(d.ApprovedScopes)),
		PolicyOverrides:  make(map[string]string, len(d.PolicyOverrides)),
		AuditLog:         append([]AuditEntry(nil), d.AuditLog...),
		slotSessions:     make(map[string]map[string]struct{}, len(d.slotSessions)),
	}
	for k, sl := range d.Slots {
		cp := *sl
		cp.Capabilities = append([]model.CapabilityEntry(nil), sl.Capabilities...)
		cp.CapabilityByID = make(map[string]model.CapabilityEntry, len(sl.CapabilityByID))
		for cid, ce := range sl.CapabilityByID {
			cp.CapabilityByID[cid] = ce
		}
		cp.AllowlistedCores = make(map[string]bool, len(sl.AllowlistedCores))
		for ac, ok := range sl.AllowlistedCores {
			cp.AllowlistedCores[ac] = ok
		}
		out.Slots[k] = &cp
	}
	for k, sv := range d.SlotViews {
		cp := *sv
		cp.StateBrief = cloneStringAnyMap(sv.StateBrief)
		cp.Methods = make(map[string]model.ActiveMethodGrant, len(sv.Methods))
		for mk, mg := range sv.Methods {
			mgCopy := mg
			if mg.Targets != nil {
				mgCopy.Targets = append([]model.TargetCandidate(nil), mg.Targets...)
			}
			cp.Methods[mk] = mgCopy
		}
		cp.SettledExecutions = make(map[string]model.OriginExecuteReply, len(sv.SettledExecutions))
		for ek, er := range sv.SettledExecutions {
			cp.SettledExecutions[ek] = er
		}
		cp.SettledOrder = append([]string(nil), sv.SettledOrder...)
		cp.LatestVisual = cloneStringAnyMap(sv.LatestVisual)
		cp.LatestAudio = cloneStringAnyMap(sv.LatestAudio)
		if sv.AllowedTimelineTags != nil {
			cp.AllowedTimelineTags = append([]string(nil), sv.AllowedTimelineTags...)
		}
		out.SlotViews[k] = &cp
	}
	for k, se := range d.Sessions {
		cp := *se
		cp.ActiveQueueID = cloneOptionalString(se.ActiveQueueID)
		cp.ConsumptionCap = cloneOptionalInt64(se.ConsumptionCap)
		if se.NegotiatedExtensions != nil {
			cp.NegotiatedExtensions = append([]string(nil), se.NegotiatedExtensions...)
		}
		permCopy := make(map[string][]string, len(se.Scope.PermittedMethods))
		for pmKey, actors := range se.Scope.PermittedMethods {
			permCopy[pmKey] = append([]string(nil), actors...)
		}
		cp.Scope.PermittedMethods = permCopy
		out.Sessions[k] = &cp
		out.indexSessionSlot(&cp)
	}
	for k, qr := range d.Queues {
		cp := *qr
		cp.ApprovedDigest = cloneOptionalString(qr.ApprovedDigest)
		cp.CurrentExecutionID = cloneOptionalString(qr.CurrentExecutionID)
		cp.Queue.Steps = append([]model.QueueStep(nil), qr.Queue.Steps...)
		cp.StepReceipts = make([]model.StepReceipt, len(qr.StepReceipts))
		for i, sr := range qr.StepReceipts {
			srCopy := sr
			srCopy.FailureReason = cloneOptionalString(sr.FailureReason)
			srCopy.EffectSummary = cloneStringAnyMap(sr.EffectSummary)
			cp.StepReceipts[i] = srCopy
		}
		out.Queues[k] = &cp
	}
	for k, v := range d.ChatDedup {
		out.ChatDedup[k] = v
	}
	for k, v := range d.ConfirmCooldowns {
		out.ConfirmCooldowns[k] = v
	}
	for k, v := range d.ApprovedScopes {
		out.ApprovedScopes[k] = v
	}
	for k, v := range d.PolicyOverrides {
		out.PolicyOverrides[k] = v
	}
	return out
}

// Atomic executes a durable ledger transaction with full rollback on error and immediate crash-safe disk persistence.
func Atomic[T any](p *PersistencePort, tx func(d *HubStoreData) (T, *model.G2AError)) (T, *model.G2AError) {
	p.mu.Lock()
	defer p.mu.Unlock()

	var zero T
	backup := p.data.Clone()

	res, gErr := tx(p.data)
	if gErr != nil {
		p.data = backup
		return zero, gErr
	}

	if p.filePath != "" {
		if err := p.persistLocked(); err != nil {
			p.data = backup
			return zero, model.NewError(model.ErrUnavailable, 503, "Failed to persist ledger")
		}
		p.dirty = false
	}
	return res, nil
}

func (p *PersistencePort) persistLocked() error {
	if err := os.MkdirAll(filepath.Dir(p.filePath), 0o755); err != nil {
		return err
	}
	out, err := json.Marshal(p.data)
	if err != nil {
		return err
	}
	tmp := p.filePath + ".tmp"
	if err := os.WriteFile(tmp, out, 0o600); err != nil {
		return err
	}
	var lastErr error
	for attempt := 0; attempt < 4; attempt++ {
		if err := os.Rename(tmp, p.filePath); err == nil {
			return nil
		} else {
			lastErr = err
			time.Sleep(2 * time.Millisecond)
		}
	}
	return lastErr
}

func ValidateArgsAgainstSchema(args map[string]any, schema map[string]any) *model.G2AError {
	return validateObjectAtDepth("", args, schema, 0)
}

func validateObjectAtDepth(prefix string, args map[string]any, schema map[string]any, depth int) *model.G2AError {
	if depth > 16 {
		return model.NewError(model.ErrInvalidArguments, 400, "Schema validation nesting depth exceeded")
	}
	if len(schema) == 0 {
		return nil
	}
	if args == nil {
		args = map[string]any{}
	}
	qualify := func(field string) string {
		if prefix == "" {
			return field
		}
		return prefix + "." + field
	}
	if reqRaw, ok := schema["required"]; ok {
		if reqList, ok := toStringSlice(reqRaw); ok {
			for _, field := range reqList {
				if _, exists := args[field]; !exists {
					return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Missing required argument %q", qualify(field)))
				}
			}
		}
	}
	props, _ := schema["properties"].(map[string]any)
	if addl, ok := schema["additionalProperties"].(bool); ok && !addl && props != nil {
		for k := range args {
			if _, declared := props[k]; !declared {
				return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Unexpected argument %q", qualify(k)))
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
		if err := validateField(qualify(k), v, propSchema, depth+1); err != nil {
			return err
		}
	}
	return nil
}

func validateField(name string, val any, schema map[string]any, depth int) *model.G2AError {
	if depth > 16 {
		return model.NewError(model.ErrInvalidArguments, 400, "Argument nesting depth exceeded")
	}
	expectedType, _ := schema["type"].(string)
	switch expectedType {
	case "string":
		s, ok := val.(string)
		if !ok {
			return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q must be string", name))
		}
		runeLen := float64(utf8.RuneCountInString(s))
		if minLen, ok := toFloat(schema["minLength"]); ok && runeLen < minLen {
			return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q shorter than minLength", name))
		}
		if maxLen, ok := toFloat(schema["maxLength"]); ok && runeLen > maxLen {
			return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q exceeds maxLength", name))
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
		arr, ok := val.([]any)
		if !ok {
			return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q must be array", name))
		}
		arrLen := float64(len(arr))
		if minItems, ok := toFloat(schema["minItems"]); ok && arrLen < minItems {
			return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q has fewer than minItems", name))
		}
		if maxItems, ok := toFloat(schema["maxItems"]); ok && arrLen > maxItems {
			return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q exceeds maxItems", name))
		}
		if itemSchema, ok := schema["items"].(map[string]any); ok {
			for idx, elem := range arr {
				if err := validateField(fmt.Sprintf("%s[%d]", name, idx), elem, itemSchema, depth+1); err != nil {
					return err
				}
			}
		}
	case "object":
		objVal, ok := val.(map[string]any)
		if !ok {
			return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q must be object", name))
		}
		if err := validateObjectAtDepth(name, objVal, schema, depth+1); err != nil {
			return err
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
	if exMin, ok := toFloat(schema["exclusiveMinimum"]); ok && num <= exMin {
		return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q must exceed exclusiveMinimum", name))
	}
	if exMax, ok := toFloat(schema["exclusiveMaximum"]); ok && num >= exMax {
		return model.NewError(model.ErrInvalidArguments, 400, fmt.Sprintf("Argument %q must be below exclusiveMaximum", name))
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
