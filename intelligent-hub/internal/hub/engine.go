package hub

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/jcs"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/model"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/store"
)

type Config struct {
	LeaseTTLMs               int64
	MinSyncIntervalMs        int64
	MaxIdleSleepIntervalMs   int64
	DefaultConfirmCooldownMs int64
	ConfirmPromptTimeoutMs   int64
	StepTransitionGraceMs    int64
	SustainedStepTimeoutMs   int64
	MaxQueueSteps            int
	AutoApproveScope         bool
	OriginSharedToken        string
	SupportedExtensions      map[string]bool
	RateLimitPerWindow       int
	RateLimitWindowMs        int64
	ClockNowMs               func() int64
}

func DefaultConfig() Config {
	return Config{
		LeaseTTLMs:               15000,
		MinSyncIntervalMs:        100,
		MaxIdleSleepIntervalMs:   10000,
		DefaultConfirmCooldownMs: 60000,
		ConfirmPromptTimeoutMs:   30000,
		StepTransitionGraceMs:    3000,
		SustainedStepTimeoutMs:   30000,
		MaxQueueSteps:            32,
		AutoApproveScope:         false,
		SupportedExtensions: map[string]bool{
			"org.g2a.narrative.timeline.v1":  true,
			"org.g2a.sim.batch_blueprint.v1": true,
			"org.g2a.commerce.catalog.v1":    true,
		},
		RateLimitPerWindow: 240,
		RateLimitWindowMs:  1000,
		ClockNowMs: func() int64 {
			return time.Now().UnixMilli()
		},
	}
}

type OriginClientPort interface {
	Execute(targetURL string, req model.OriginExecuteRequest) (model.OriginExecuteReply, *model.G2AError)
}

type HTTPOriginClient struct {
	Client      *http.Client
	SharedToken string
}

type rawOriginReplyEnvelope struct {
	Acceptance model.AcceptanceState `json:"acceptance"`
	Result     json.RawMessage       `json:"result"`
	Error      *model.G2AError       `json:"error"`
}

func NewHTTPOriginClient(sharedToken string) *HTTPOriginClient {
	transport := &http.Transport{
		Proxy:               nil,
		MaxIdleConns:        64,
		MaxIdleConnsPerHost: 32,
		IdleConnTimeout:     90 * time.Second,
	}
	return &HTTPOriginClient{
		Client:      &http.Client{Timeout: 10 * time.Second, Transport: transport},
		SharedToken: sharedToken,
	}
}

func (c *HTTPOriginClient) Execute(targetURL string, req model.OriginExecuteRequest) (model.OriginExecuteReply, *model.G2AError) {
	var zero model.OriginExecuteReply
	if targetURL == "" {
		return zero, model.NewError(model.ErrUnavailable, 503, "Origin execute endpoint is not registered for slot")
	}
	payloadBytes, err := json.Marshal(req)
	if err != nil {
		return zero, model.NewError(model.ErrInvalidRequest, 400, err.Error())
	}
	env := model.RequestEnvelope{
		G2AVersion:  model.WireVersion,
		RequestID:   generateID("req"),
		TimestampMs: time.Now().UnixMilli(),
		Operation:   "origin.execute",
		Payload:     payloadBytes,
		Extensions:  map[string]any{},
	}
	body, _ := json.Marshal(env)
	httpReq, err := http.NewRequest(http.MethodPost, targetURL, bytes.NewReader(body))
	if err != nil {
		return zero, model.NewError(model.ErrUnavailable, 503, err.Error())
	}
	httpReq.Header.Set("Content-Type", "application/json")
	if c.SharedToken != "" {
		httpReq.Header.Set("Authorization", "Bearer "+c.SharedToken)
	}
	resp, err := c.Client.Do(httpReq)
	if err != nil {
		return zero, model.NewError(model.ErrUnavailable, 503, "Origin unreachable: "+err.Error())
	}
	defer resp.Body.Close()
	respBytes, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return zero, model.NewError(model.ErrOutcomeUnknown, 504, "Failed reading Origin response")
	}
	var replyEnv rawOriginReplyEnvelope
	if err := json.Unmarshal(respBytes, &replyEnv); err != nil {
		return zero, model.NewError(model.ErrUnavailable, 503, "Malformed Origin reply envelope")
	}
	if replyEnv.Acceptance != model.AcceptanceAccepted {
		if replyEnv.Error != nil {
			return zero, replyEnv.Error
		}
		return zero, model.NewError(model.ErrUnavailable, 503, "Origin rejected execution")
	}
	var out model.OriginExecuteReply
	if err := json.Unmarshal(replyEnv.Result, &out); err != nil {
		return zero, model.NewError(model.ErrUnavailable, 503, "Malformed OriginExecuteReply")
	}
	return out, nil
}

// EventStreamHub manages SSE / push subscribers per session_id with bounded session history.
type EventStreamHub struct {
	mu           sync.Mutex
	subscribers  map[string]map[chan model.EventEnvelope]struct{}
	history      map[string][]model.EventEnvelope
	historyOrder []string
}

func NewEventStreamHub() *EventStreamHub {
	return &EventStreamHub{
		subscribers:  make(map[string]map[chan model.EventEnvelope]struct{}),
		history:      make(map[string][]model.EventEnvelope),
		historyOrder: make([]string, 0),
	}
}

func (b *EventStreamHub) PushEvent(sessionID, eventName string, payload any) {
	ev := model.EventEnvelope{
		G2AVersion:  model.WireVersion,
		EventID:     generateID("ev"),
		SessionID:   sessionID,
		TimestampMs: time.Now().UnixMilli(),
		Event:       eventName,
		Payload:     payload,
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if _, tracked := b.history[sessionID]; !tracked {
		b.historyOrder = append(b.historyOrder, sessionID)
		guard := len(b.historyOrder)
		for len(b.history) >= 256 && guard > 0 {
			guard--
			evict := b.historyOrder[0]
			b.historyOrder = b.historyOrder[1:]
			if evict == sessionID || len(b.subscribers[evict]) > 0 {
				b.historyOrder = append(b.historyOrder, evict)
				continue
			}
			delete(b.history, evict)
			delete(b.subscribers, evict)
		}
		if cap(b.historyOrder) > 1024 && len(b.historyOrder) <= 300 {
			b.historyOrder = append([]string(nil), b.historyOrder...)
		}
	}
	hist := append(b.history[sessionID], ev)
	if len(hist) > 64 {
		hist = append([]model.EventEnvelope(nil), hist[len(hist)-64:]...)
	}
	b.history[sessionID] = hist
	for ch := range b.subscribers[sessionID] {
		select {
		case ch <- ev:
		default:
		}
	}
}

func (b *EventStreamHub) Subscribe(sessionID string, replayPending bool) (chan model.EventEnvelope, func()) {
	return b.SubscribeAfter(sessionID, replayPending, "")
}

func (b *EventStreamHub) SubscribeAfter(sessionID string, replayPending bool, lastEventID string) (chan model.EventEnvelope, func()) {
	ch := make(chan model.EventEnvelope, 64)
	b.mu.Lock()
	if b.subscribers[sessionID] == nil {
		b.subscribers[sessionID] = make(map[chan model.EventEnvelope]struct{})
	}
	b.subscribers[sessionID][ch] = struct{}{}
	hist := b.history[sessionID]
	if lastEventID != "" {
		foundIdx := -1
		for idx, ev := range hist {
			if ev.EventID == lastEventID {
				foundIdx = idx
				break
			}
		}
		if foundIdx >= 0 {
			for _, ev := range hist[foundIdx+1:] {
				ch <- ev
			}
		} else if replayPending {
			for _, ev := range hist {
				ch <- ev
			}
		}
	} else if replayPending {
		for _, ev := range hist {
			ch <- ev
		}
	}
	b.mu.Unlock()

	cancel := func() {
		b.mu.Lock()
		if subs, ok := b.subscribers[sessionID]; ok {
			delete(subs, ch)
		}
		b.mu.Unlock()
	}
	return ch, cancel
}

// TrustedConsentBroker isolates human player consent & confirmation prompts away from the Core Interface.
type PendingScopePrompt struct {
	PromptID    string            `json:"prompt_id"`
	ScopeDigest string            `json:"scope_digest"`
	Scope       model.FrozenScope `json:"scope"`
	replyCh     chan ScopeDecision
}

type ScopeDecision struct {
	Approved         bool
	RememberAutoJoin bool
}

type PendingQueuePrompt struct {
	SessionID                string            `json:"session_id"`
	QueueID                  string            `json:"queue_id"`
	QueueDigest              string            `json:"queue_digest"`
	Fingerprint              string            `json:"fingerprint"`
	IntentSummary            string            `json:"intent_summary"`
	Steps                    []model.QueueStep `json:"steps"`
	RequireNativeRealMoneyUI bool              `json:"require_native_real_money_ui"`
}

type PendingResumePrompt struct {
	SessionID string `json:"session_id"`
}

type TrustedConsentBroker struct {
	mu           sync.Mutex
	autoScope    bool
	scopePrompts map[string]*PendingScopePrompt
	queuePrompts map[string]*PendingQueuePrompt  // key: session_id|queue_id
	resumes      map[string]*PendingResumePrompt // key: session_id
	onChange     func()
}

func NewTrustedConsentBroker(autoScope bool) *TrustedConsentBroker {
	return &TrustedConsentBroker{
		autoScope:    autoScope,
		scopePrompts: make(map[string]*PendingScopePrompt),
		queuePrompts: make(map[string]*PendingQueuePrompt),
		resumes:      make(map[string]*PendingResumePrompt),
	}
}

func (c *TrustedConsentBroker) SetOnChange(fn func()) {
	c.mu.Lock()
	c.onChange = fn
	c.mu.Unlock()
}

func (c *TrustedConsentBroker) notifyChanged() {
	c.mu.Lock()
	fn := c.onChange
	c.mu.Unlock()
	if fn != nil {
		fn()
	}
}

func (c *TrustedConsentBroker) PromptSessionScope(scope model.FrozenScope, scopeDigest string, timeout time.Duration) ScopeDecision {
	if c.autoScope {
		return ScopeDecision{Approved: true}
	}
	pid := generateID("scope-prompt")
	ch := make(chan ScopeDecision, 1)
	c.mu.Lock()
	c.scopePrompts[pid] = &PendingScopePrompt{
		PromptID:    pid,
		ScopeDigest: scopeDigest,
		Scope:       scope,
		replyCh:     ch,
	}
	c.mu.Unlock()
	c.notifyChanged()

	select {
	case dec := <-ch:
		return dec
	case <-time.After(timeout):
		c.mu.Lock()
		delete(c.scopePrompts, pid)
		c.mu.Unlock()
		c.notifyChanged()
		return ScopeDecision{Approved: false}
	}
}

func (c *TrustedConsentBroker) DecideScope(promptID string, approved bool, rememberAutoJoin bool) (string, bool) {
	c.mu.Lock()
	p, ok := c.scopePrompts[promptID]
	if ok {
		delete(c.scopePrompts, promptID)
	}
	c.mu.Unlock()
	if !ok {
		return "", false
	}
	c.notifyChanged()
	p.replyCh <- ScopeDecision{Approved: approved, RememberAutoJoin: rememberAutoJoin}
	return p.ScopeDigest, true
}

func (c *TrustedConsentBroker) RegisterQueuePrompt(p PendingQueuePrompt) {
	c.mu.Lock()
	c.queuePrompts[store.QueueKey(p.SessionID, p.QueueID)] = &p
	c.mu.Unlock()
	c.notifyChanged()
}

func (c *TrustedConsentBroker) PopQueuePrompt(sessionID, queueID string) (*PendingQueuePrompt, bool) {
	c.mu.Lock()
	k := store.QueueKey(sessionID, queueID)
	p, ok := c.queuePrompts[k]
	if ok {
		delete(c.queuePrompts, k)
	}
	c.mu.Unlock()
	if ok {
		c.notifyChanged()
	}
	return p, ok
}

func (c *TrustedConsentBroker) RegisterResumePrompt(sessionID string) {
	c.mu.Lock()
	c.resumes[sessionID] = &PendingResumePrompt{SessionID: sessionID}
	c.mu.Unlock()
	c.notifyChanged()
}

func (c *TrustedConsentBroker) PopResumePrompt(sessionID string) bool {
	c.mu.Lock()
	_, ok := c.resumes[sessionID]
	if ok {
		delete(c.resumes, sessionID)
	}
	c.mu.Unlock()
	if ok {
		c.notifyChanged()
	}
	return ok
}

func (c *TrustedConsentBroker) SnapshotPending() map[string]any {
	c.mu.Lock()
	defer c.mu.Unlock()
	scopes := make([]PendingScopePrompt, 0, len(c.scopePrompts))
	for _, s := range c.scopePrompts {
		scopes = append(scopes, *s)
	}
	sort.Slice(scopes, func(i, j int) bool { return scopes[i].PromptID < scopes[j].PromptID })
	queues := make([]PendingQueuePrompt, 0, len(c.queuePrompts))
	for _, q := range c.queuePrompts {
		queues = append(queues, *q)
	}
	sort.Slice(queues, func(i, j int) bool { return queues[i].QueueID < queues[j].QueueID })
	resumes := make([]PendingResumePrompt, 0, len(c.resumes))
	for _, r := range c.resumes {
		resumes = append(resumes, *r)
	}
	sort.Slice(resumes, func(i, j int) bool { return resumes[i].SessionID < resumes[j].SessionID })
	return map[string]any{
		"scope_prompts":  scopes,
		"queue_prompts":  queues,
		"resume_prompts": resumes,
	}
}

// Engine implements HubEngine from Chapter 3 of G2A_PSEUDOCODE_API.
type Engine struct {
	cfg            Config
	persistence    *store.PersistencePort
	originClient   OriginClientPort
	outboundOrigin *OriginOutboundStreamBroker
	pushStream     *EventStreamHub
	consent        *TrustedConsentBroker

	signalMu sync.Mutex
	signalCh chan struct{}

	dashMu   sync.Mutex
	dashSubs map[chan struct{}]struct{}
}

func NewEngine(cfg Config, persistence *store.PersistencePort, originClient OriginClientPort, pushStream *EventStreamHub, consent *TrustedConsentBroker) *Engine {
	if cfg.ClockNowMs == nil {
		cfg.ClockNowMs = func() int64 { return time.Now().UnixMilli() }
	}
	outboundBroker := NewOriginOutboundStreamBroker(10 * time.Second)
	if originClient == nil {
		originClient = NewHybridOriginClient(cfg.OriginSharedToken, outboundBroker)
	}
	if pushStream == nil {
		pushStream = NewEventStreamHub()
	}
	if consent == nil {
		consent = NewTrustedConsentBroker(cfg.AutoApproveScope)
	}
	eng := &Engine{
		cfg:            cfg,
		persistence:    persistence,
		originClient:   originClient,
		outboundOrigin: outboundBroker,
		pushStream:     pushStream,
		consent:        consent,
		signalCh:       make(chan struct{}),
		dashSubs:       make(map[chan struct{}]struct{}),
	}
	consent.SetOnChange(eng.NotifyDashboard)
	return eng
}

func (e *Engine) PushStream() *EventStreamHub                 { return e.pushStream }
func (e *Engine) Consent() *TrustedConsentBroker              { return e.consent }
func (e *Engine) Persistence() *store.PersistencePort         { return e.persistence }
func (e *Engine) OutboundOrigin() *OriginOutboundStreamBroker { return e.outboundOrigin }

func (e *Engine) SubscribeDashboard() (chan struct{}, func()) {
	ch := make(chan struct{}, 1)
	e.dashMu.Lock()
	e.dashSubs[ch] = struct{}{}
	e.dashMu.Unlock()
	return ch, func() {
		e.dashMu.Lock()
		delete(e.dashSubs, ch)
		e.dashMu.Unlock()
	}
}

func (e *Engine) NotifyDashboard() {
	e.dashMu.Lock()
	defer e.dashMu.Unlock()
	for ch := range e.dashSubs {
		select {
		case ch <- struct{}{}:
		default:
		}
	}
}

func (e *Engine) broadcastWakeup() {
	e.signalMu.Lock()
	close(e.signalCh)
	e.signalCh = make(chan struct{})
	e.signalMu.Unlock()
	e.NotifyDashboard()
}

func (e *Engine) waitWakeup(timeout time.Duration) {
	e.signalMu.Lock()
	ch := e.signalCh
	e.signalMu.Unlock()
	select {
	case <-ch:
	case <-time.After(timeout):
	}
}

// --- Origin Interface Handlers ---

func (e *Engine) HandleOriginCapabilities(payload model.OriginCapabilitiesPayload) *model.G2AError {
	sortedCaps := make([]model.CapabilityEntry, len(payload.Capabilities))
	copy(sortedCaps, payload.Capabilities)
	sort.Slice(sortedCaps, func(i, j int) bool {
		return sortedCaps[i].MethodID < sortedCaps[j].MethodID
	})
	verifiedDigest, err := jcs.DigestSHA256("g2a.cap.v1", sortedCaps)
	if err != nil {
		return model.NewError(model.ErrInvalidRequest, 400, err.Error())
	}
	if verifiedDigest != payload.CapabilityTableDigest {
		return model.NewError(model.ErrInvalidArguments, 400, "Capability table digest mismatch")
	}
	// FRAND non-discriminatory allowlist transparency (§7.3): companion slots with an allowlist MUST publish an http(s) criteria URL.
	criteriaURL := strings.TrimSpace(payload.AllowlistCriteriaURL)
	if payload.RoleKind == "companion" && len(payload.AllowlistedCores) > 0 {
		if !strings.HasPrefix(criteriaURL, "https://") && !strings.HasPrefix(criteriaURL, "http://") {
			return model.NewError(model.ErrInvalidArguments, 400, "FRAND rule (§7.3): companion slot allowlist requires a public http(s) allowlist_criteria_url")
		}
	}

	sanitizedCaps := make([]model.CapabilityEntry, len(sortedCaps))
	copy(sanitizedCaps, sortedCaps)
	if payload.RoleKind == "independent_npc" {
		for i := range sanitizedCaps {
			filtered := make([]string, 0, len(sanitizedCaps[i].PermittedActors))
			for _, a := range sanitizedCaps[i].PermittedActors {
				if a != "player" {
					filtered = append(filtered, a)
				}
			}
			sanitizedCaps[i].PermittedActors = filtered
		}
	}

	type capUpdateOutcome struct {
		notifySessions []string
		closedSessions []string
		cancels        []cancelTarget
	}
	outcome, gErr := store.Atomic(e.persistence, func(d *store.HubStoreData) (capUpdateOutcome, *model.G2AError) {
		var out capUpdateOutcome
		slot, exists := d.Slots[payload.SlotID]
		prevDigest := ""
		if !exists {
			slot = &store.SlotRecord{
				SlotID:           payload.SlotID,
				PlayerOnline:     true,
				AllowlistedCores: make(map[string]bool),
			}
			d.Slots[payload.SlotID] = slot
		} else {
			prevDigest = slot.CapabilityTableDigest
		}
		slot.RoleKind = payload.RoleKind
		slot.BillingMode = payload.BillingMode
		slot.Disabled = payload.Disabled
		slot.MaxSessions = payload.MaxSessions
		slot.SharedCooldownMs = payload.SharedCooldownMs
		slot.AllowlistCriteriaURL = criteriaURL
		if payload.PlayerID != "" {
			slot.PlayerID = payload.PlayerID
		} else if slot.PlayerID == "" {
			slot.PlayerID = "player-local"
		}
		if payload.GameInstanceID != "" {
			slot.GameInstanceID = payload.GameInstanceID
		} else if slot.GameInstanceID == "" {
			slot.GameInstanceID = "game-local"
		}
		if payload.RoomID != "" {
			slot.RoomID = payload.RoomID
		}
		if payload.FactionID != "" {
			slot.FactionID = payload.FactionID
		}
		if payload.OriginExecuteURL != "" {
			slot.OriginExecuteURL = payload.OriginExecuteURL
		}
		slot.Capabilities = sanitizedCaps
		slot.CapabilityByID = make(map[string]model.CapabilityEntry, len(sanitizedCaps))
		for _, c := range sanitizedCaps {
			slot.CapabilityByID[c.MethodID] = c
		}
		slot.CapabilityTableDigest = verifiedDigest
		if len(payload.AllowlistedCores) > 0 {
			slot.AllowlistEnabled = true
			slot.AllowlistedCores = make(map[string]bool, len(payload.AllowlistedCores))
			for _, cid := range payload.AllowlistedCores {
				slot.AllowlistedCores[cid] = true
			}
		} else {
			slot.AllowlistEnabled = false
			slot.AllowlistedCores = make(map[string]bool)
		}
		if _, hasView := d.SlotViews[payload.SlotID]; !hasView {
			d.SlotViews[payload.SlotID] = &store.SlotView{
				SlotID:            payload.SlotID,
				Epoch:             1,
				Rev:               0,
				StateBrief:        map[string]any{},
				Methods:           map[string]model.ActiveMethodGrant{},
				SettledExecutions: map[string]model.OriginExecuteReply{},
			}
		}

		// Competitive / ranked mode slot disabling (§4.3): fence-close any active sessions immediately!
		if payload.Disabled {
			for _, sess := range d.SessionsForSlot(payload.SlotID) {
				if sess.State != "closed" {
					out.cancels = append(out.cancels, e.cancelAndFenceSessionQueuesInTx(d, sess)...)
					sess.State = "closed"
					out.closedSessions = append(out.closedSessions, sess.SessionID)
				}
			}
			if len(out.closedSessions) > 0 {
				d.PruneClosedSessions()
			}
			return out, nil
		}

		// Mid-session dynamic capability refresh (§3.3 & §4.1):
		// Intersect existing FrozenScope downward (never silently expanding unapproved methods/actors!)
		// and notify live sessions of the updated capability_table_digest.
		if prevDigest != "" && prevDigest != verifiedDigest {
			for _, sess := range d.SessionsForSlot(payload.SlotID) {
				if sess.State == "closed" {
					continue
				}
				narrowedPerm := make(map[string][]string, len(sess.Scope.PermittedMethods))
				for mID, prevActors := range sess.Scope.PermittedMethods {
					capDef, stillExists := slot.CapabilityByID[mID]
					if !stillExists {
						continue
					}
					kept := make([]string, 0, len(prevActors))
					for _, a := range prevActors {
						if containsString(capDef.PermittedActors, a) {
							kept = append(kept, a)
						}
					}
					if len(kept) > 0 {
						sort.Strings(kept)
						narrowedPerm[mID] = kept
					}
				}
				sess.Scope.PermittedMethods = narrowedPerm
				sess.Scope.CapabilityTableDigest = verifiedDigest
				if newSD, errSD := jcs.DigestSHA256("g2a.scope.v1", sess.Scope); errSD == nil {
					sess.ScopeDigest = newSD
				}
				if sess.PlayerOnline && sess.State == "active" {
					out.notifySessions = append(out.notifySessions, sess.SessionID)
				}
			}
		}
		return out, nil
	})
	if gErr == nil {
		if len(outcome.cancels) > 0 || len(outcome.closedSessions) > 0 {
			e.broadcastWakeup()
		}
		for _, c := range outcome.cancels {
			_, _ = e.originClient.Execute(c.targetURL, model.OriginExecuteRequest{
				SlotID: c.slotID, Epoch: c.epoch, Kind: "cancel", ExecutionID: c.execID, Args: map[string]any{},
			})
		}
		for _, sid := range outcome.closedSessions {
			e.pushStream.PushEvent(sid, "session.closed", map[string]any{"reason": "slot_disabled_by_origin"})
		}
		for _, sid := range outcome.notifySessions {
			e.pushStream.PushEvent(sid, "capabilities.updated", map[string]any{
				"slot_id":                 payload.SlotID,
				"capability_table_digest": verifiedDigest,
				"capability_table":        sanitizedCaps,
			})
		}
		e.NotifyDashboard()
	}
	return gErr
}

func (e *Engine) HandleOriginSync(frame model.OriginSyncFrame) *model.G2AError {
	type pendingPush struct {
		sessionID string
		ctx       model.ProjectedContext
	}
	txFn := func(d *store.HubStoreData) ([]pendingPush, *model.G2AError) {
		view, ok := d.SlotViews[frame.SlotID]
		if !ok {
			if frame.Mode != "full" {
				return nil, model.NewError(model.ErrStaleContext, 409, "Origin slot view uninitialized; resend full frame")
			}
			view = &store.SlotView{
				SlotID:            frame.SlotID,
				Epoch:             frame.Epoch,
				StateBrief:        map[string]any{},
				Methods:           map[string]model.ActiveMethodGrant{},
				SettledExecutions: map[string]model.OriginExecuteReply{},
			}
			d.SlotViews[frame.SlotID] = view
		}
		if view.SettledExecutions == nil {
			view.SettledExecutions = make(map[string]model.OriginExecuteReply)
		}

		if frame.Mode == "full" || frame.Epoch > view.Epoch {
			oldEpoch := view.Epoch
			view.Epoch = frame.Epoch
			view.Rev = frame.Rev
			view.LastSignificantRev = frame.Rev
			view.StateBrief = frame.StateBrief
			view.Methods = make(map[string]model.ActiveMethodGrant, len(frame.UpsertedMethods))
			for _, g := range frame.UpsertedMethods {
				view.Methods[store.MethodKey(g.MethodID, g.Actor)] = g
			}
			if frame.Epoch > oldEpoch {
				e.abortObsoleteEpochQueuesInTx(d, frame.SlotID, frame.Epoch)
			}
			if frame.ExtensionsData != nil {
				view.ExtensionsData = make(map[string]map[string]any, len(frame.ExtensionsData))
				for ek, ev := range frame.ExtensionsData {
					cp := make(map[string]any, len(ev))
					for mk, mv := range ev {
						cp[mk] = mv
					}
					view.ExtensionsData[ek] = cp
				}
			} else {
				view.ExtensionsData = nil
			}
		} else {
			if frame.Epoch != view.Epoch || frame.BaseRev == nil || *frame.BaseRev != view.Rev {
				return nil, model.NewError(model.ErrStaleContext, 409, "Origin delta base_rev mismatch; resend full frame")
			}
			view.Rev = frame.Rev
			if frame.SignificantEvent {
				view.LastSignificantRev = frame.Rev
			}
			view.StateBrief = frame.StateBrief
			for _, rem := range frame.RemovedMethods {
				delete(view.Methods, store.MethodKey(rem.MethodID, rem.Actor))
			}
			for _, g := range frame.UpsertedMethods {
				view.Methods[store.MethodKey(g.MethodID, g.Actor)] = g
			}
			if frame.ExtensionsData != nil {
				if view.ExtensionsData == nil {
					view.ExtensionsData = make(map[string]map[string]any, len(frame.ExtensionsData))
				}
				for ek, ev := range frame.ExtensionsData {
					cp := make(map[string]any, len(ev))
					for mk, mv := range ev {
						cp[mk] = mv
					}
					view.ExtensionsData[ek] = cp
				}
			}
		}

		// Atomically record optional multimodal frames, timeline tags, and bounded settled sustained-step executions!
		if frame.VisualRef != nil {
			view.LatestVisual = frame.VisualRef
		}
		if frame.AudioRef != nil {
			view.LatestAudio = frame.AudioRef
		}
		if frame.AllowedTimelineTags != nil {
			view.AllowedTimelineTags = append([]string(nil), frame.AllowedTimelineTags...)
		}
		for _, settled := range frame.SettledExecutions {
			view.RecordSettledExecution(settled)
		}

		// Adaptive quiet-period sleep throttling (§3.4 & Pseudocode §3.3):
		// Push immediately on significant_event, OR wake up at throttled backoff cadence once IdleBackoffMs has elapsed.
		now := e.cfg.ClockNowMs()
		outPushes := make([]pendingPush, 0)
		for _, sess := range d.ActiveSessionsForSlot(frame.SlotID) {
			if frame.SignificantEvent {
				sess.IdleBackoffMs = e.cfg.MinSyncIntervalMs
				sess.NextAllowedPullMs = 0
				sess.LastContextPushMs = now
				outPushes = append(outPushes, pendingPush{
					sessionID: sess.SessionID,
					ctx:       e.projectContextInTx(d, sess, nil),
				})
			} else {
				backoff := sess.IdleBackoffMs
				if backoff < e.cfg.MinSyncIntervalMs {
					backoff = e.cfg.MinSyncIntervalMs
				}
				// Never emit unsolicited quiet-period wakeup pushes mid-queue while ActiveQueueID is executing;
				// QueueReceipt already returns LatestContext upon settlement!
				if sess.ActiveQueueID == nil && sess.LastContextPushMs > 0 && now-sess.LastContextPushMs >= backoff {
					sess.LastContextPushMs = now
					outPushes = append(outPushes, pendingPush{
						sessionID: sess.SessionID,
						ctx:       e.projectContextInTx(d, sess, nil),
					})
				}
				next := backoff * 2
				if next > e.cfg.MaxIdleSleepIntervalMs {
					next = e.cfg.MaxIdleSleepIntervalMs
				}
				sess.IdleBackoffMs = next
			}
		}
		return outPushes, nil
	}

	var (
		pushes []pendingPush
		gErr   *model.G2AError
	)
	if frame.Mode == "full" {
		pushes, gErr = store.Atomic(e.persistence, txFn)
	} else {
		pushes, gErr = store.MutateVolatile(e.persistence, txFn)
	}
	if gErr != nil {
		return gErr
	}

	e.broadcastWakeup()
	for _, p := range pushes {
		e.pushStream.PushEvent(p.sessionID, "context.push", p.ctx)
	}
	return nil
}

type cancelTarget struct {
	targetURL string
	slotID    string
	epoch     int64
	execID    string
}

func (e *Engine) HandleOriginPlayerEvent(ev model.OriginPlayerEvent) *model.G2AError {
	switch ev.Kind {
	case "presence_changed":
		e.OnPlayerPresenceChanged(ev.SlotID, ev.Online, ev.RemainingOnlineHumans)
		return nil

	case "emergency_stop":
		cancels, gErr := store.Atomic(e.persistence, func(d *store.HubStoreData) ([]cancelTarget, *model.G2AError) {
			var list []cancelTarget
			for _, sess := range d.ActiveSessionsForSlot(ev.SlotID) {
				list = append(list, e.cancelAndFenceSessionQueuesInTx(d, sess)...)
			}
			return list, nil
		})
		if gErr != nil {
			return gErr
		}
		e.broadcastWakeup()
		for _, c := range cancels {
			_, _ = e.originClient.Execute(c.targetURL, model.OriginExecuteRequest{
				SlotID: c.slotID, Epoch: c.epoch, Kind: "cancel", ExecutionID: c.execID, Args: map[string]any{},
			})
		}
		return nil

	case "player_command":
		type cmdPush struct {
			sessionID string
			ctx       model.ProjectedContext
		}
		pushes, gErr := store.MutateVolatile(e.persistence, func(d *store.HubStoreData) ([]cmdPush, *model.G2AError) {
			now := e.cfg.ClockNowMs()
			cmd := ev.Text
			var list []cmdPush
			for _, sess := range d.ActiveSessionsForSlot(ev.SlotID) {
				sess.IdleBackoffMs = e.cfg.MinSyncIntervalMs
				sess.NextAllowedPullMs = 0
				sess.LastContextPushMs = now
				list = append(list, cmdPush{
					sessionID: sess.SessionID,
					ctx:       e.projectContextInTx(d, sess, &cmd),
				})
			}
			return list, nil
		})
		if gErr != nil {
			return gErr
		}
		for _, p := range pushes {
			e.pushStream.PushEvent(p.sessionID, "context.push", p.ctx)
		}
		return nil

	case "queue_approval":
		_, gErr := store.ReadOnly(e.persistence, func(d *store.HubStoreData) (struct{}, *model.G2AError) {
			rec, ok := d.Queues[store.QueueKey(ev.SessionID, ev.QueueID)]
			if !ok || rec.RequiresRealMoneyUI {
				return struct{}{}, model.NewError(model.ErrPermissionDenied, 403, "Real-money confirmation requires native platform security UI")
			}
			return struct{}{}, nil
		})
		if gErr != nil {
			return gErr
		}
		e.consent.PopQueuePrompt(ev.SessionID, ev.QueueID)
		e.OnTrustedConfirmationResult(ev.SessionID, ev.QueueID, ev.QueueDigest, ev.Fingerprint, ev.Approved)
		return nil

	case "host_handoff":
		return e.HandoffMeteredNPCSlot(ev.SlotID, ev.NewPlayerID)

	default:
		return model.NewError(model.ErrInvalidArguments, 400, "Unknown player_event kind: "+ev.Kind)
	}
}

// HandoffMeteredNPCSlot implements G2A V0.0.1 Section 4.3 privacy-isolated host handoff for player_metered independent_npc slots:
// stops billing and closes all sessions/queues bound to the previous host before rebinding the NPC slot to newPlayerID.
func (e *Engine) HandoffMeteredNPCSlot(slotID, newPlayerID string) *model.G2AError {
	newPlayerID = strings.TrimSpace(newPlayerID)
	if slotID == "" || newPlayerID == "" {
		return model.NewError(model.ErrInvalidArguments, 400, "slot_id and new_player_id are required for host_handoff")
	}
	type handoffResult struct {
		cancels     []cancelTarget
		closedIDs   []string
		poppedPairs [][2]string
	}
	res, gErr := store.Atomic(e.persistence, func(d *store.HubStoreData) (handoffResult, *model.G2AError) {
		var zr handoffResult
		slot, ok := d.Slots[slotID]
		if !ok {
			return zr, model.NewError(model.ErrNotFound, 404, "Unknown slot_id")
		}
		if slot.RoleKind != "independent_npc" || slot.BillingMode != "player_metered" {
			return zr, model.NewError(model.ErrPermissionDenied, 403, "host_handoff is only permitted on player_metered independent_npc slots")
		}
		prevHost := slot.PlayerID
		if prevHost != newPlayerID {
			for _, sess := range d.SessionsForSlot(slotID) {
				for _, rec := range d.Queues {
					if rec.SessionID == sess.SessionID && rec.State == "awaiting_confirmation" {
						zr.poppedPairs = append(zr.poppedPairs, [2]string{sess.SessionID, rec.QueueID})
					}
				}
				if sess.State != "closed" {
					zr.cancels = append(zr.cancels, e.cancelAndFenceSessionQueuesInTx(d, sess)...)
					sess.State = "closed"
					sess.PlayerOnline = false
					zr.closedIDs = append(zr.closedIDs, sess.SessionID)
				}
			}
			slot.PlayerID = newPlayerID
			if len(zr.closedIDs) > 0 {
				d.PruneClosedSessions()
			}
		}
		slot.PlayerOnline = true
		d.AppendAudit(e.cfg.ClockNowMs(), "slot.npc_host_handoff", slotID, "", "",
			fmt.Sprintf("Metered NPC host handed off from %s to %s", prevHost, newPlayerID))
		return zr, nil
	})
	if gErr != nil {
		return gErr
	}
	for _, pair := range res.poppedPairs {
		e.consent.PopQueuePrompt(pair[0], pair[1])
	}
	for _, sid := range res.closedIDs {
		e.consent.PopResumePrompt(sid)
	}
	e.broadcastWakeup()
	for _, c := range res.cancels {
		_, _ = e.originClient.Execute(c.targetURL, model.OriginExecuteRequest{
			SlotID: c.slotID, Epoch: c.epoch, Kind: "cancel", ExecutionID: c.execID, Args: map[string]any{},
		})
	}
	for _, sid := range res.closedIDs {
		e.pushStream.PushEvent(sid, "session.closed", map[string]any{"reason": "npc_host_handoff"})
	}
	return nil
}

func (e *Engine) OnPlayerPresenceChanged(slotID string, online bool, remainingOnlineHumans int) {
	closedSessions, _ := store.Atomic(e.persistence, func(d *store.HubStoreData) ([]string, *model.G2AError) {
		slot, ok := d.Slots[slotID]
		var targetRoomID string
		if ok {
			slot.PlayerOnline = online
			targetRoomID = slot.RoomID
		}
		var closed []string
		if !online {
			for _, sess := range d.SessionsForSlot(slotID) {
				sess.PlayerOnline = false
				shouldStop := (remainingOnlineHumans == 0) ||
					(sess.Scope.RoleKind == "companion") ||
					(sess.Scope.BillingMode == "player_metered")
				if shouldStop && sess.State != "closed" {
					e.clearUndispatchedQueuesInTx(d, sess)
					sess.State = "closed"
					closed = append(closed, sess.SessionID)
				}
			}
			// Multiplayer room rule: when remainingOnlineHumans drops to 0 in a room, stop ALL shared developer_sponsored NPCs in that room too!
			if remainingOnlineHumans == 0 && targetRoomID != "" {
				for otherSlotID, otherSlot := range d.Slots {
					if otherSlotID == slotID || otherSlot.RoomID != targetRoomID {
						continue
					}
					otherSlot.PlayerOnline = false
					for _, roomSess := range d.SessionsForSlot(otherSlotID) {
						roomSess.PlayerOnline = false
						if roomSess.State != "closed" {
							e.clearUndispatchedQueuesInTx(d, roomSess)
							roomSess.State = "closed"
							closed = append(closed, roomSess.SessionID)
						}
					}
				}
			}
		} else {
			for _, sess := range d.SessionsForSlot(slotID) {
				if sess.State != "closed" {
					sess.PlayerOnline = true
				}
			}
		}
		if len(closed) > 0 {
			d.PruneClosedSessions()
		}
		return closed, nil
	})
	e.broadcastWakeup()
	for _, sid := range closedSessions {
		e.pushStream.PushEvent(sid, "session.closed", map[string]any{"reason": "player_offline"})
	}
}

// --- Core Interface Handlers ---

func (e *Engine) HandleSessionAuthorize(g2aVersion, callerCoreID string, req model.AuthorizeRequest) (model.AuthorizeReply, *model.G2AError) {
	var zero model.AuthorizeReply
	if g2aVersion != model.WireVersion {
		return zero, model.NewError(model.ErrUnsupportedVersion, 400, "Require g2a_version 0.0.1")
	}
	var negotiatedExts []string
	for _, ext := range req.Extensions {
		if e.cfg.SupportedExtensions[ext.Name] {
			negotiatedExts = append(negotiatedExts, ext.Name)
		} else if ext.Required {
			return zero, model.NewError(model.ErrUnsupportedCapability, 400, "Unsupported required extension: "+ext.Name)
		}
	}
	sort.Strings(negotiatedExts)

	type prepInfo struct {
		scope         model.FrozenScope
		scopeDigest   string
		capDigest     string
		preApproved   bool
		caps          []model.CapabilityEntry
	}

	prep, gErr := store.ReadOnly(e.persistence, func(d *store.HubStoreData) (prepInfo, *model.G2AError) {
		var zp prepInfo
		slot, ok := d.Slots[req.SlotID]
		if !ok {
			return zp, model.NewError(model.ErrNotFound, 404, "Unknown slot_id")
		}
		if !slot.PlayerOnline {
			return zp, model.NewError(model.ErrPermissionDenied, 403, "Associated player is offline")
		}
		if slot.Disabled {
			return zp, model.NewError(model.ErrPermissionDenied, 403, "Slot is disabled for the current game mode")
		}
		// Multiplayer faction & personal-seat confidentiality isolation (Section 2.2 Row 2)
		if slot.FactionID != "" && req.FactionID != "" && req.FactionID != slot.FactionID {
			return zp, model.NewError(model.ErrPermissionDenied, 403, "Cross-faction slot authorization is forbidden")
		}
		if slot.RoleKind == "companion" && req.PlayerID != "" && req.PlayerID != slot.PlayerID {
			return zp, model.NewError(model.ErrPermissionDenied, 403, "Personal companion slot belongs to another player")
		}
		if slot.AllowlistEnabled && !slot.AllowlistedCores[callerCoreID] {
			return zp, model.NewError(model.ErrPermissionDenied, 403, "Core not allowlisted on this slot")
		}

		narrowed := intersectScopeWithCapabilities(req.RequestedScope, slot, callerCoreID)
		sDigest, err := jcs.DigestSHA256("g2a.scope.v1", narrowed)
		if err != nil {
			return zp, model.NewError(model.ErrInvalidRequest, 400, err.Error())
		}
		return prepInfo{
			scope:       narrowed,
			scopeDigest: sDigest,
			capDigest:   slot.CapabilityTableDigest,
			preApproved: d.ApprovedScopes[sDigest],
			caps:        slot.Capabilities,
		}, nil
	})
	if gErr != nil {
		return zero, gErr
	}

	rememberScope := false
	if !prep.preApproved {
		dec := e.consent.PromptSessionScope(prep.scope, prep.scopeDigest, 30*time.Second)
		if !dec.Approved {
			return zero, model.NewError(model.ErrPermissionDenied, 403, "Player declined authorization")
		}
		rememberScope = dec.RememberAutoJoin
	}

	return store.Atomic(e.persistence, func(d *store.HubStoreData) (model.AuthorizeReply, *model.G2AError) {
		var zr model.AuthorizeReply
		slot, ok := d.Slots[req.SlotID]
		if !ok || !slot.PlayerOnline || slot.Disabled {
			return zr, model.NewError(model.ErrPermissionDenied, 403, "Player went offline or slot was disabled during consent")
		}
		if slot.MaxSessions > 0 {
			otherLive := 0
			for _, existingSess := range d.SessionsForSlot(req.SlotID) {
				if existingSess.State == "closed" {
					continue
				}
				if existingSess.CallerCoreID == callerCoreID {
					_ = e.cancelAndFenceSessionQueuesInTx(d, existingSess)
					existingSess.State = "closed"
					continue
				}
				otherLive++
			}
			if otherLive >= slot.MaxSessions {
				return zr, model.NewError(model.ErrResourceLimit, 429, "Slot active session limit reached")
			}
		}
		if rememberScope {
			d.ApprovedScopes[prep.scopeDigest] = true
		}
		sessionID := generateID("sess")
		now := e.cfg.ClockNowMs()
		d.RegisterSession(&store.SessionRecord{
			SessionID:              sessionID,
			CallerCoreID:           callerCoreID,
			Scope:                  prep.scope,
			ScopeDigest:            prep.scopeDigest,
			State:                  "active",
			PlayerOnline:           true,
			LocalFallbackActivated: false,
			LeaseDeadlineMs:        now + e.cfg.LeaseTTLMs,
			IdleBackoffMs:          e.cfg.MinSyncIntervalMs,
			NextAllowedPullMs:      0,
			LastContextPushMs:      now,
			ConsumptionUsed:        0,
			ConsumptionCap:         req.ConsumptionCap,
			NegotiatedExtensions:   negotiatedExts,
		})
		d.AppendAudit(now, "session.authorized", req.SlotID, sessionID, "", fmt.Sprintf("Core %s connected to %s", callerCoreID, req.SlotID))
		var tableToSend []model.CapabilityEntry
		if req.CachedCapabilityDigest == nil || *req.CachedCapabilityDigest != prep.capDigest {
			tableToSend = prep.caps
		}
		return model.AuthorizeReply{
			SessionID:             sessionID,
			ScopeDigest:           prep.scopeDigest,
			FrozenScope:           prep.scope,
			CapabilityTableDigest: prep.capDigest,
			CapabilityTable:       tableToSend,
			NegotiatedExtensions:  negotiatedExts,
		}, nil
	})
}

func (e *Engine) HandleSessionHeartbeat(callerCoreID, sessionID string) (model.HeartbeatReply, *model.G2AError) {
	return store.MutateVolatile(e.persistence, func(d *store.HubStoreData) (model.HeartbeatReply, *model.G2AError) {
		var zero model.HeartbeatReply
		sess, gErr := d.GetSessionForCaller(sessionID, callerCoreID)
		if gErr != nil {
			return zero, gErr
		}
		if !sess.PlayerOnline || sess.State == "closed" {
			return zero, model.NewError(model.ErrRevoked, 403, "Player offline or session closed")
		}
		sess.LeaseDeadlineMs = e.cfg.ClockNowMs() + e.cfg.LeaseTTLMs
		return model.HeartbeatReply{LeaseDeadlineMs: sess.LeaseDeadlineMs}, nil
	})
}

func (e *Engine) HandleSessionResume(callerCoreID, sessionID string) (model.ResumeReply, *model.G2AError) {
	needPrompt := false
	reply, gErr := store.Atomic(e.persistence, func(d *store.HubStoreData) (model.ResumeReply, *model.G2AError) {
		var zero model.ResumeReply
		sess, err := d.GetSessionForCaller(sessionID, callerCoreID)
		if err != nil {
			return zero, err
		}
		if !sess.PlayerOnline || sess.State == "closed" {
			return zero, model.NewError(model.ErrRevoked, 403, "Session closed or player offline")
		}
		now := e.cfg.ClockNowMs()
		ctx := e.projectContextInTx(d, sess, nil)
		if now < sess.LeaseDeadlineMs && !sess.LocalFallbackActivated && sess.State != "paused_awaiting_player" {
			sess.State = "active"
			sess.LeaseDeadlineMs = now + e.cfg.LeaseTTLMs
			return model.ResumeReply{ActionsPaused: false, LatestContext: &ctx}, nil
		}
		sess.State = "paused_awaiting_player"
		sess.LeaseDeadlineMs = now + e.cfg.LeaseTTLMs
		needPrompt = true
		return model.ResumeReply{ActionsPaused: true, LatestContext: &ctx}, nil
	})
	if gErr != nil {
		return reply, gErr
	}
	if needPrompt {
		e.consent.RegisterResumePrompt(sessionID)
	}
	return reply, nil
}

func (e *Engine) OnTrustedResumeDecision(sessionID string, approved bool) {
	e.consent.PopResumePrompt(sessionID)
	type resumeOutcome struct {
		kind string
		ctx  model.ProjectedContext
	}
	out, _ := store.Atomic(e.persistence, func(d *store.HubStoreData) (resumeOutcome, *model.G2AError) {
		var zo resumeOutcome
		sess, ok := d.Sessions[sessionID]
		if !ok || sess.State != "paused_awaiting_player" {
			return zo, nil
		}
		if approved && sess.PlayerOnline {
			sess.State = "active"
			sess.LocalFallbackActivated = false
			sess.LeaseDeadlineMs = e.cfg.ClockNowMs() + e.cfg.LeaseTTLMs
			return resumeOutcome{kind: "resumed", ctx: e.projectContextInTx(d, sess, nil)}, nil
		}
		sess.State = "closed"
		d.PruneClosedSessions()
		return resumeOutcome{kind: "closed"}, nil
	})
	if out.kind == "resumed" {
		e.pushStream.PushEvent(sessionID, "session.resumed", out.ctx)
	} else if out.kind == "closed" {
		e.pushStream.PushEvent(sessionID, "session.closed", map[string]any{"reason": "resume_declined"})
	}
}

func (e *Engine) HandleSessionClose(callerCoreID, sessionID string) *model.G2AError {
	cancels, gErr := store.Atomic(e.persistence, func(d *store.HubStoreData) ([]cancelTarget, *model.G2AError) {
		sess, err := d.GetSessionForCaller(sessionID, callerCoreID)
		if err != nil {
			return nil, err
		}
		var list []cancelTarget
		if sess.State != "closed" {
			list = e.cancelAndFenceSessionQueuesInTx(d, sess)
			sess.State = "closed"
			d.PruneClosedSessions()
		}
		return list, nil
	})
	if gErr != nil {
		return gErr
	}
	e.broadcastWakeup()
	for _, c := range cancels {
		_, _ = e.originClient.Execute(c.targetURL, model.OriginExecuteRequest{
			SlotID: c.slotID, Epoch: c.epoch, Kind: "cancel", ExecutionID: c.execID, Args: map[string]any{},
		})
	}
	return nil
}

func (e *Engine) HandleContextPull(callerCoreID, sessionID string) (model.ProjectedContext, *model.G2AError) {
	return store.MutateVolatile(e.persistence, func(d *store.HubStoreData) (model.ProjectedContext, *model.G2AError) {
		var zero model.ProjectedContext
		sess, gErr := d.GetSessionForCaller(sessionID, callerCoreID)
		if gErr != nil {
			return zero, gErr
		}
		if !sess.PlayerOnline || sess.State != "active" {
			return zero, model.NewError(model.ErrPermissionDenied, 403, "Session not active")
		}
		now := e.cfg.ClockNowMs()
		if now < sess.NextAllowedPullMs {
			wait := sess.NextAllowedPullMs - now
			err := model.NewError(model.ErrResourceLimit, 429, "Adaptive idle sleep active")
			err.RetryAfterMs = &wait
			return zero, err
		}
		sess.NextAllowedPullMs = now + sess.IdleBackoffMs
		return e.projectContextInTx(d, sess, nil), nil
	})
}

func (e *Engine) HandleChatSend(callerCoreID, sessionID string, msg model.ChatMessage) *model.G2AError {
	if strings.TrimSpace(msg.MessageID) == "" {
		return model.NewError(model.ErrInvalidArguments, 400, "message_id is required")
	}
	if strings.TrimSpace(msg.Text) == "" || utf8.RuneCountInString(msg.Text) > 4096 {
		return model.NewError(model.ErrInvalidArguments, 400, "chat text must be non-empty and at most 4096 characters")
	}
	audience := msg.Audience
	if audience == "" {
		audience = "player_only"
	}
	if audience != "player_only" && audience != "party" && audience != "public" {
		return model.NewError(model.ErrInvalidArguments, 400, "audience must be player_only, party, or public")
	}
	type chatDispatch struct {
		url string
		req model.OriginExecuteRequest
	}
	disp, gErr := store.Atomic(e.persistence, func(d *store.HubStoreData) (*chatDispatch, *model.G2AError) {
		sess, err := d.GetSessionForCaller(sessionID, callerCoreID)
		if err != nil {
			return nil, err
		}
		if !sess.PlayerOnline || sess.State != "active" || !sess.Scope.AllowChat {
			return nil, model.NewError(model.ErrPermissionDenied, 403, "Chat not authorized or player offline")
		}
		dedupKey := sessionID + "|" + msg.MessageID
		if d.RecordChatDedup(dedupKey) {
			return nil, nil
		}
		slot := d.Slots[sess.Scope.SlotID]
		view := d.SlotViews[sess.Scope.SlotID]
		return &chatDispatch{
			url: slot.OriginExecuteURL,
			req: model.OriginExecuteRequest{
				SlotID:      sess.Scope.SlotID,
				Epoch:       view.Epoch,
				Kind:        "deliver_chat",
				ExecutionID: "chat-" + msg.MessageID,
				Args: map[string]any{
					"audience":      audience,
					"text":          msg.Text,
					"ai_provenance": true,
				},
			},
		}, nil
	})
	if gErr != nil || disp == nil {
		return gErr
	}
	_, gErr = e.originClient.Execute(disp.url, disp.req)
	return gErr
}

// --- QueueScheduler (Section 3.4) ---

func resolveStepReadiness(grant *model.ActiveMethodGrant, args map[string]any) *string {
	if grant == nil {
		return nil
	}
	if len(grant.Targets) == 0 {
		r := grant.Readiness
		return &r
	}
	if tidRaw, ok := args["target_id"]; ok {
		if tid, isStr := tidRaw.(string); isStr {
			for _, cand := range grant.Targets {
				if cand.TargetID == tid {
					r := cand.Readiness
					return &r
				}
			}
			return nil
		}
	}
	r := grant.Readiness
	return &r
}

func computeTargetFingerprint(steps []model.QueueStep) string {
	canon, _ := jcs.DigestSHA256("g2a.fp.v1", steps)
	return canon
}

type submitDecision struct {
	kind                  string // "early_receipt" | "still_awaiting" | "needs_confirm" | "ready_dispatch"
	receipt               model.QueueReceipt
	prompt                PendingQueuePrompt
	cooldownMs            int64
}

func (e *Engine) HandleQueueSubmit(callerCoreID, sessionID string, q model.MethodCallQueue) (model.QueueReceipt, *model.G2AError) {
	var zero model.QueueReceipt
	if strings.TrimSpace(q.QueueID) == "" {
		return zero, model.NewError(model.ErrInvalidArguments, 400, "queue_id is required")
	}
	if q.Trigger != "player_command" && q.Trigger != "proactive" {
		return zero, model.NewError(model.ErrInvalidArguments, 400, "trigger must be player_command or proactive")
	}
	// Lazily expire unanswered confirmation prompts before evaluating queue deduplication & refusal cooldowns (§5.2 Item 2).
	e.ReapExpiredConfirmationsOnce()

	queueDigest, err := jcs.DigestSHA256("g2a.confirm.v1", map[string]any{
		"session_id": sessionID,
		"queue_id":   q.QueueID,
		"steps":      q.Steps,
	})
	if err != nil {
		return zero, model.NewError(model.ErrInvalidRequest, 400, err.Error())
	}

	decision, gErr := store.Atomic(e.persistence, func(d *store.HubStoreData) (submitDecision, *model.G2AError) {
		var zd submitDecision
		sess, sErr := d.GetSessionForCaller(sessionID, callerCoreID)
		if sErr != nil {
			return zd, sErr
		}
		if !sess.PlayerOnline || sess.State != "active" {
			return zd, model.NewError(model.ErrPermissionDenied, 403, "Session inactive, paused, or player offline")
		}
		if sess.ConsumptionCap != nil && sess.ConsumptionUsed >= *sess.ConsumptionCap {
			return zd, model.NewError(model.ErrResourceLimit, 429, "Player consumption cap reached")
		}

		view, ok := d.SlotViews[sess.Scope.SlotID]
		if !ok {
			return zd, model.NewError(model.ErrUnavailable, 503, "Slot view not initialized")
		}
		if q.Epoch != view.Epoch || q.BasedOnRev > view.Rev || q.BasedOnRev < view.LastSignificantRev {
			fresh := e.projectContextInTx(d, sess, nil)
			stErr := model.NewError(model.ErrStaleContext, 409, "Epoch or based_on_rev expired")
			stErr.FreshContext = &fresh
			return zd, stErr
		}
		if len(q.Steps) == 0 || len(q.Steps) > e.cfg.MaxQueueSteps {
			return zd, model.NewError(model.ErrInvalidArguments, 400, "Invalid step count")
		}

		now := e.cfg.ClockNowMs()
		d.SweepExpiredCooldowns(now)
		slot := d.Slots[sess.Scope.SlotID]

		// 1. Pre-execution anti-replay barrier: exact deduplication on (session_id, queue_id)
		qKey := store.QueueKey(sessionID, q.QueueID)
		if existing, exists := d.Queues[qKey]; exists {
			if existing.QueueDigest != queueDigest {
				return zd, model.NewError(model.ErrConflict, 409, "queue_id reused with different steps or arguments")
			}
			if existing.State != "awaiting_confirmation" {
				return submitDecision{
					kind:    "early_receipt",
					receipt: e.toReceiptInTx(d, sess, existing),
				}, nil
			}
			if existing.ApprovedDigest == nil || *existing.ApprovedDigest != queueDigest {
				return submitDecision{
					kind:       "still_awaiting",
					cooldownMs: e.cfg.DefaultConfirmCooldownMs,
				}, nil
			}
			if until, inSlotCd := d.SlotCooldowns[sess.Scope.SlotID]; inSlotCd && now < until {
				wait := until - now
				scErr := model.NewError(model.ErrResourceLimit, 429, "Slot shared FRAND cooldown active")
				scErr.RetryAfterMs = &wait
				scErr.CooldownMs = &wait
				return zd, scErr
			}
			if slot != nil && slot.SharedCooldownMs > 0 {
				d.SlotCooldowns[sess.Scope.SlotID] = now + slot.SharedCooldownMs
			}
			existing.State = "running"
			return submitDecision{kind: "ready_dispatch"}, nil
		}

		// 2. Validate scope, tri-state policy, target-level readiness, FRAND shared cooldown, and refusal cooldown
		if until, inSlotCd := d.SlotCooldowns[sess.Scope.SlotID]; inSlotCd && now < until {
			wait := until - now
			scErr := model.NewError(model.ErrResourceLimit, 429, "Slot shared FRAND cooldown active")
			scErr.RetryAfterMs = &wait
			scErr.CooldownMs = &wait
			return zd, scErr
		}
		needsConfirm := false
		requiresRealMoneyUI := false
		fp := computeTargetFingerprint(q.Steps)
		cdKey := sessionID + "|" + fp
		if until, inCd := d.ConfirmCooldowns[cdKey]; inCd && now < until {
			wait := until - now
			cdErr := model.NewError(model.ErrResourceLimit, 429, "Target confirmation is in refusal cooldown")
			cdErr.RetryAfterMs = &wait
			cdErr.CooldownMs = &wait
			return zd, cdErr
		}
		for idx, step := range q.Steps {
			if step.StepIndex != idx {
				return zd, model.NewError(model.ErrInvalidArguments, 400, "step_index must be contiguous starting at 0")
			}
			allowedActors := sess.Scope.PermittedMethods[step.MethodID]
			if !containsString(allowedActors, step.Actor) {
				return zd, model.NewError(model.ErrPermissionDenied, 403, "Method or actor outside Scope")
			}
			if sess.Scope.RoleKind == "independent_npc" && step.Actor == "player" {
				return zd, model.NewError(model.ErrPermissionDenied, 403, "NPC cannot target player seat")
			}

			capDef, hasCap := slot.CapabilityByID[step.MethodID]
			if !hasCap {
				return zd, model.NewError(model.ErrInvalidArguments, 400, "Unknown method_id: "+step.MethodID)
			}
			if vErr := store.ValidateArgsAgainstSchema(step.Args, capDef.ParamsSchema); vErr != nil {
				return zd, vErr
			}

			effectivePolicy := capDef.DefaultPolicy
			if effectivePolicy == "" {
				effectivePolicy = "allow"
			}
			if override, hasOverride := d.PolicyOverrides[store.PolicyKey(sess.Scope.SlotID, step.MethodID, step.Actor)]; hasOverride {
				effectivePolicy = override
			}
			if containsString(capDef.RiskFlags, "real_money") {
				effectivePolicy = "confirm"
				requiresRealMoneyUI = true
			} else if q.Trigger == "proactive" && step.Actor == "player" && effectivePolicy == "allow" {
				effectivePolicy = "confirm"
			}
			if effectivePolicy == "prohibit" {
				return zd, model.NewError(model.ErrPermissionDenied, 403, "Prohibited by policy")
			}
			if effectivePolicy == "confirm" {
				needsConfirm = true
			}

			var grantPtr *model.ActiveMethodGrant
			if g, hasGrant := view.Methods[store.MethodKey(step.MethodID, step.Actor)]; hasGrant {
				gCopy := g
				grantPtr = &gCopy
			}
			stepReadiness := resolveStepReadiness(grantPtr, step.Args)
			if stepReadiness == nil {
				fresh := e.projectContextInTx(d, sess, nil)
				stErr := model.NewError(model.ErrStaleContext, 409, "Method or target not currently available")
				stErr.FreshContext = &fresh
				return zd, stErr
			}
			if step.StepIndex == 0 && *stepReadiness != "ready" {
				fresh := e.projectContextInTx(d, sess, nil)
				stErr := model.NewError(model.ErrStaleContext, 409, "First step target is not ready")
				stErr.FreshContext = &fresh
				return zd, stErr
			}
		}

		// 3. Persist ledger entry BEFORE returning 409 confirmation_required outside transaction!
		receipts := make([]model.StepReceipt, len(q.Steps))
		for i, s := range q.Steps {
			receipts[i] = model.StepReceipt{
				StepIndex: s.StepIndex,
				MethodID:  s.MethodID,
				Actor:     s.Actor,
				State:     "pending",
			}
		}
		initialState := "running"
		var confirmDeadline int64
		if needsConfirm {
			initialState = "awaiting_confirmation"
			if e.cfg.ConfirmPromptTimeoutMs > 0 {
				confirmDeadline = now + e.cfg.ConfirmPromptTimeoutMs
			}
		}
		rec := &store.QueueLedgerRecord{
			SessionID:           sessionID,
			QueueID:             q.QueueID,
			QueueDigest:         queueDigest,
			Fingerprint:         fp,
			Queue:               q,
			State:               initialState,
			RequiresRealMoneyUI: requiresRealMoneyUI,
			ConfirmDeadlineMs:   confirmDeadline,
			StepReceipts:        receipts,
		}
		d.RegisterQueue(qKey, rec)

		if needsConfirm {
			d.AppendAudit(now, "queue.confirm_hold", sess.Scope.SlotID, sessionID, q.QueueID, q.IntentSummary)
			return submitDecision{
				kind:       "needs_confirm",
				cooldownMs: e.cfg.DefaultConfirmCooldownMs,
				prompt: PendingQueuePrompt{
					SessionID:                sessionID,
					QueueID:                  q.QueueID,
					QueueDigest:              queueDigest,
					Fingerprint:              fp,
					IntentSummary:            q.IntentSummary,
					Steps:                    q.Steps,
					RequireNativeRealMoneyUI: requiresRealMoneyUI,
				},
			}, nil
		}
		if slot != nil && slot.SharedCooldownMs > 0 {
			d.SlotCooldowns[sess.Scope.SlotID] = now + slot.SharedCooldownMs
		}
		d.AppendAudit(now, "queue.accepted", sess.Scope.SlotID, sessionID, q.QueueID, q.IntentSummary)
		return submitDecision{kind: "ready_dispatch"}, nil
	})
	if gErr != nil {
		return zero, gErr
	}

	switch decision.kind {
	case "early_receipt":
		return decision.receipt, nil
	case "still_awaiting":
		cd := decision.cooldownMs
		errHold := model.NewError(model.ErrConfirmationRequired, 409, "Still awaiting player confirmation")
		errHold.CooldownMs = &cd
		return zero, errHold
	case "needs_confirm":
		e.consent.RegisterQueuePrompt(decision.prompt)
		cd := decision.cooldownMs
		errHold := model.NewError(model.ErrConfirmationRequired, 409, "Awaiting single-prompt player confirmation")
		errHold.CooldownMs = &cd
		return zero, errHold
	default:
		return e.dispatchQueueSteps(sessionID, q.QueueID)
	}
}

func (e *Engine) OnTrustedConfirmationResult(sessionID, queueID, queueDigest, fingerprint string, approved bool) {
	e.consent.PopQueuePrompt(sessionID, queueID)
	eventName := ""
	var eventPayload map[string]any

	_, _ = store.Atomic(e.persistence, func(d *store.HubStoreData) (struct{}, *model.G2AError) {
		rec, ok := d.Queues[store.QueueKey(sessionID, queueID)]
		if !ok || rec.State != "awaiting_confirmation" {
			return struct{}{}, nil
		}
		if queueDigest == "" {
			queueDigest = rec.QueueDigest
		}
		if fingerprint == "" {
			fingerprint = rec.Fingerprint
		}
		if approved && queueDigest == rec.QueueDigest {
			dCopy := queueDigest
			rec.ApprovedDigest = &dCopy
			eventName = "queue.confirmed"
			eventPayload = map[string]any{"queue_id": queueID, "queue_digest": queueDigest}
		} else {
			rec.State = "cancelled"
			for i := range rec.StepReceipts {
				if rec.StepReceipts[i].State == "pending" {
					rec.StepReceipts[i].State = "cancelled"
				}
			}
			d.ConfirmCooldowns[sessionID+"|"+fingerprint] = e.cfg.ClockNowMs() + e.cfg.DefaultConfirmCooldownMs
			eventName = "queue.declined"
			eventPayload = map[string]any{
				"queue_id":    queueID,
				"cooldown_ms": e.cfg.DefaultConfirmCooldownMs,
			}
		}
		return struct{}{}, nil
	})
	if eventName != "" {
		e.pushStream.PushEvent(sessionID, eventName, eventPayload)
	}
}

func (e *Engine) preemptOrAcquireSlotQueue(sessionID, queueID string) *model.G2AError {
	toCancel, gErr := store.Atomic(e.persistence, func(d *store.HubStoreData) (*cancelTarget, *model.G2AError) {
		sess := d.Sessions[sessionID]
		incoming := d.Queues[store.QueueKey(sessionID, queueID)]
		var cancelOut *cancelTarget
		if sess.ActiveQueueID != nil && *sess.ActiveQueueID != queueID {
			prev, ok := d.Queues[store.QueueKey(sessionID, *sess.ActiveQueueID)]
			if ok && prev.State == "running" {
				if !incoming.Queue.Supersedes {
					return nil, model.NewError(model.ErrConflict, 409, "Another queue is already running on this slot; set supersedes=true to preempt")
				}
				prev.State = "aborted"
				for i := range prev.StepReceipts {
					if prev.StepReceipts[i].State == "pending" {
						prev.StepReceipts[i].State = "aborted"
					} else if prev.StepReceipts[i].State == "executing" {
						prev.StepReceipts[i].State = "cancelled"
					}
				}
				if prev.CurrentExecutionID != nil {
					slot := d.Slots[sess.Scope.SlotID]
					view := d.SlotViews[sess.Scope.SlotID]
					cancelOut = &cancelTarget{
						targetURL: slot.OriginExecuteURL,
						slotID:    sess.Scope.SlotID,
						epoch:     view.Epoch,
						execID:    *prev.CurrentExecutionID,
					}
					prev.CurrentExecutionID = nil
				}
			}
		}
		qidCopy := queueID
		sess.ActiveQueueID = &qidCopy
		return cancelOut, nil
	})
	if gErr != nil {
		return gErr
	}
	if toCancel != nil {
		e.broadcastWakeup()
		_, _ = e.originClient.Execute(toCancel.targetURL, model.OriginExecuteRequest{
			SlotID: toCancel.slotID, Epoch: toCancel.epoch, Kind: "cancel", ExecutionID: toCancel.execID, Args: map[string]any{},
		})
	}
	return nil
}

type stepPrepOutcome struct {
	kind      string // "ready" | "abort" | "wait"
	targetURL string
	execReq   model.OriginExecuteRequest
	reason    string
}

func (e *Engine) waitUntilStepReadyOrAbort(sessionID, queueID string, stepIndex int) stepPrepOutcome {
	grace := int64(0)
	if stepIndex > 0 {
		grace = e.cfg.StepTransitionGraceMs
	}
	deadlineMs := e.cfg.ClockNowMs() + grace

	for {
		out, _ := store.MutateVolatile(e.persistence, func(d *store.HubStoreData) (stepPrepOutcome, *model.G2AError) {
			sess := d.Sessions[sessionID]
			rec := d.Queues[store.QueueKey(sessionID, queueID)]
			view := d.SlotViews[sess.Scope.SlotID]
			slot := d.Slots[sess.Scope.SlotID]

			if !sess.PlayerOnline || sess.State != "active" || rec.State != "running" || rec.Queue.Epoch != view.Epoch {
				return stepPrepOutcome{kind: "abort", reason: "session_or_queue_interrupted"}, nil
			}
			if sess.ConsumptionCap != nil && sess.ConsumptionUsed >= *sess.ConsumptionCap {
				return stepPrepOutcome{kind: "abort", reason: "consumption_cap_reached"}, nil
			}
			step := rec.Queue.Steps[stepIndex]
			var grantPtr *model.ActiveMethodGrant
			if g, ok := view.Methods[store.MethodKey(step.MethodID, step.Actor)]; ok {
				gCopy := g
				grantPtr = &gCopy
			}
			r := resolveStepReadiness(grantPtr, step.Args)
			if r != nil && *r == "ready" {
				execID := generateID("exec")
				rec.CurrentExecutionID = &execID
				rec.StepReceipts[stepIndex].State = "executing"
				sess.ConsumptionUsed++
				mID := step.MethodID
				act := step.Actor
				return stepPrepOutcome{
					kind:      "ready",
					targetURL: slot.OriginExecuteURL,
					execReq: model.OriginExecuteRequest{
						SlotID:      sess.Scope.SlotID,
						Epoch:       view.Epoch,
						Kind:        "invoke",
						ExecutionID: execID,
						MethodID:    &mID,
						Actor:       &act,
						Args:        step.Args,
					},
				}, nil
			}
			if r == nil {
				return stepPrepOutcome{kind: "abort", reason: "target_disappeared_or_invalid"}, nil
			}
			return stepPrepOutcome{kind: "wait"}, nil
		})

		if out.kind == "ready" || out.kind == "abort" {
			return out
		}
		if e.cfg.ClockNowMs() >= deadlineMs {
			return stepPrepOutcome{kind: "abort", reason: "prerequisite_did_not_turn_ready"}
		}
		e.waitWakeup(25 * time.Millisecond)
	}
}

func (e *Engine) awaitStepSettlementOrCancel(sessionID, queueID, executionID, targetURL, slotID string, epoch int64) model.OriginExecuteReply {
	deadlineMs := e.cfg.ClockNowMs() + e.cfg.SustainedStepTimeoutMs
	for {
		settled, done := func() (model.OriginExecuteReply, bool) {
			res, _ := store.MutateVolatile(e.persistence, func(d *store.HubStoreData) (*model.OriginExecuteReply, *model.G2AError) {
				sess := d.Sessions[sessionID]
				rec := d.Queues[store.QueueKey(sessionID, queueID)]
				view := d.SlotViews[sess.Scope.SlotID]
				if rep, ok := view.SettledExecutions[executionID]; ok {
					delete(view.SettledExecutions, executionID)
					return &rep, nil
				}
				if rec.State != "running" || !sess.PlayerOnline {
					reason := "queue_interrupted_or_cancelled"
					return &model.OriginExecuteReply{
						ExecutionID:   executionID,
						State:         "cancelled",
						FailureReason: &reason,
					}, nil
				}
				return nil, nil
			})
			if res != nil {
				return *res, true
			}
			return model.OriginExecuteReply{}, false
		}()
		if done {
			return settled
		}
		if e.cfg.ClockNowMs() >= deadlineMs {
			// Halt the timed-out sustained step at the Interaction Origin so no zombie in-flight task continues in-game!
			_, _ = e.originClient.Execute(targetURL, model.OriginExecuteRequest{
				SlotID: slotID, Epoch: epoch, Kind: "cancel", ExecutionID: executionID, Args: map[string]any{},
			})
			reason := "sustained_step_timed_out"
			return model.OriginExecuteReply{
				ExecutionID:   executionID,
				State:         "failed",
				FailureReason: &reason,
			}
		}
		e.waitWakeup(25 * time.Millisecond)
	}
}

func (e *Engine) dispatchQueueSteps(sessionID, queueID string) (model.QueueReceipt, *model.G2AError) {
	var zero model.QueueReceipt
	if gErr := e.preemptOrAcquireSlotQueue(sessionID, queueID); gErr != nil {
		return zero, gErr
	}

	stepCount, _ := store.ReadOnly(e.persistence, func(d *store.HubStoreData) (int, *model.G2AError) {
		return len(d.Queues[store.QueueKey(sessionID, queueID)].Queue.Steps), nil
	})

	for i := 0; i < stepCount; i++ {
		prep := e.waitUntilStepReadyOrAbort(sessionID, queueID, i)
		if prep.kind == "abort" {
			return e.abortRemainingSteps(sessionID, queueID, i, prep.reason), nil
		}

		execReply, gErr := e.originClient.Execute(prep.targetURL, prep.execReq)
		if gErr != nil {
			reason := gErr.Message
			execReply = model.OriginExecuteReply{
				ExecutionID:   prep.execReq.ExecutionID,
				State:         "failed",
				FailureReason: &reason,
			}
		} else if execReply.State == "executing" {
			execReply = e.awaitStepSettlementOrCancel(
				sessionID, queueID, execReply.ExecutionID, prep.targetURL, prep.execReq.SlotID, prep.execReq.Epoch,
			)
		}

		e.recordStepOutcome(sessionID, queueID, i, execReply)
		if execReply.State != "succeeded" {
			reason := "step_not_succeeded"
			if execReply.FailureReason != nil {
				reason = *execReply.FailureReason
			}
			return e.abortRemainingSteps(sessionID, queueID, i+1, reason), nil
		}
	}

	return e.finalizeCompletedQueue(sessionID, queueID), nil
}

func (e *Engine) recordStepOutcome(sessionID, queueID string, stepIdx int, rep model.OriginExecuteReply) {
	// Volatile in-memory step update coalesced by PersistencePort; terminal queue state is persisted atomically in finalize/abort.
	_, _ = store.MutateVolatile(e.persistence, func(d *store.HubStoreData) (struct{}, *model.G2AError) {
		rec, ok := d.Queues[store.QueueKey(sessionID, queueID)]
		if !ok {
			return struct{}{}, nil
		}
		rec.CurrentExecutionID = nil
		rec.StepReceipts[stepIdx].State = rep.State
		rec.StepReceipts[stepIdx].EffectSummary = rep.EffectSummary
		rec.StepReceipts[stepIdx].FailureReason = rep.FailureReason
		return struct{}{}, nil
	})
}

func (e *Engine) abortRemainingSteps(sessionID, queueID string, fromIndex int, reason string) model.QueueReceipt {
	receipt, _ := store.Atomic(e.persistence, func(d *store.HubStoreData) (model.QueueReceipt, *model.G2AError) {
		sess := d.Sessions[sessionID]
		rec := d.Queues[store.QueueKey(sessionID, queueID)]
		rec.CurrentExecutionID = nil
		if rec.State == "running" {
			rec.State = "aborted"
		}
		for idx := fromIndex; idx < len(rec.StepReceipts); idx++ {
			if rec.StepReceipts[idx].State == "pending" || rec.StepReceipts[idx].State == "executing" {
				rec.StepReceipts[idx].State = "aborted"
				rCopy := reason
				rec.StepReceipts[idx].FailureReason = &rCopy
			}
		}
		if sess.ActiveQueueID != nil && *sess.ActiveQueueID == queueID {
			sess.ActiveQueueID = nil
		}
		sess.LastContextPushMs = e.cfg.ClockNowMs()
		d.PruneTerminalQueues()
		return e.toReceiptInTx(d, sess, rec), nil
	})
	return receipt
}

func (e *Engine) finalizeCompletedQueue(sessionID, queueID string) model.QueueReceipt {
	receipt, _ := store.Atomic(e.persistence, func(d *store.HubStoreData) (model.QueueReceipt, *model.G2AError) {
		sess := d.Sessions[sessionID]
		rec := d.Queues[store.QueueKey(sessionID, queueID)]
		rec.CurrentExecutionID = nil
		if rec.State == "running" {
			rec.State = "completed"
		}
		if sess.ActiveQueueID != nil && *sess.ActiveQueueID == queueID {
			sess.ActiveQueueID = nil
		}
		sess.LastContextPushMs = e.cfg.ClockNowMs()
		d.PruneTerminalQueues()
		return e.toReceiptInTx(d, sess, rec), nil
	})
	return receipt
}

func (e *Engine) HandleQueueCancel(callerCoreID, sessionID, queueID string) (model.QueueReceipt, *model.G2AError) {
	var toCancel *cancelTarget
	receipt, gErr := store.Atomic(e.persistence, func(d *store.HubStoreData) (model.QueueReceipt, *model.G2AError) {
		var zero model.QueueReceipt
		sess, err := d.GetSessionForCaller(sessionID, callerCoreID)
		if err != nil {
			return zero, err
		}
		rec, ok := d.Queues[store.QueueKey(sessionID, queueID)]
		if !ok {
			return zero, model.NewError(model.ErrNotFound, 404, "Unknown queue_id")
		}
		if rec.State == "awaiting_confirmation" || rec.State == "running" {
			if rec.CurrentExecutionID != nil {
				slot := d.Slots[sess.Scope.SlotID]
				view := d.SlotViews[sess.Scope.SlotID]
				toCancel = &cancelTarget{
					targetURL: slot.OriginExecuteURL,
					slotID:    sess.Scope.SlotID,
					epoch:     view.Epoch,
					execID:    *rec.CurrentExecutionID,
				}
				rec.CurrentExecutionID = nil
			}
			rec.State = "cancelled"
			for i := range rec.StepReceipts {
				if rec.StepReceipts[i].State == "pending" || rec.StepReceipts[i].State == "executing" {
					rec.StepReceipts[i].State = "cancelled"
				}
			}
			if sess.ActiveQueueID != nil && *sess.ActiveQueueID == queueID {
				sess.ActiveQueueID = nil
			}
		}
		return e.toReceiptInTx(d, sess, rec), nil
	})
	if gErr != nil {
		return receipt, gErr
	}
	e.consent.PopQueuePrompt(sessionID, queueID)
	e.broadcastWakeup()
	if toCancel != nil {
		_, _ = e.originClient.Execute(toCancel.targetURL, model.OriginExecuteRequest{
			SlotID: toCancel.slotID, Epoch: toCancel.epoch, Kind: "cancel", ExecutionID: toCancel.execID, Args: map[string]any{},
		})
	}
	return receipt, nil
}

func (e *Engine) HandleQueueQuery(callerCoreID, sessionID, queueID string) (model.QueueReceipt, *model.G2AError) {
	return store.ReadOnly(e.persistence, func(d *store.HubStoreData) (model.QueueReceipt, *model.G2AError) {
		var zero model.QueueReceipt
		sess, err := d.GetSessionForCaller(sessionID, callerCoreID)
		if err != nil {
			return zero, err
		}
		rec, ok := d.Queues[store.QueueKey(sessionID, queueID)]
		if !ok {
			return zero, model.NewError(model.ErrNotFound, 404, "Unknown queue_id")
		}
		return e.toReceiptInTx(d, sess, rec), nil
	})
}

// --- Trusted Player Governance, Policy Overrides, Lease Reaper & Dashboard Status ---

func (e *Engine) SetPolicyOverride(slotID, methodID, actor, policy string) *model.G2AError {
	if policy != "allow" && policy != "confirm" && policy != "prohibit" {
		return model.NewError(model.ErrInvalidArguments, 400, "policy must be allow, confirm, or prohibit")
	}
	_, gErr := store.Atomic(e.persistence, func(d *store.HubStoreData) (struct{}, *model.G2AError) {
		slot, ok := d.Slots[slotID]
		if !ok {
			return struct{}{}, model.NewError(model.ErrNotFound, 404, "Unknown slot_id")
		}
		capDef, hasCap := slot.CapabilityByID[methodID]
		if !hasCap {
			return struct{}{}, model.NewError(model.ErrNotFound, 404, "Unknown method_id")
		}
		if containsString(capDef.RiskFlags, "real_money") && policy == "allow" {
			return struct{}{}, model.NewError(model.ErrPermissionDenied, 403, "real_money capability cannot be downgraded to auto-allow")
		}
		d.PolicyOverrides[store.PolicyKey(slotID, methodID, actor)] = policy
		d.AppendAudit(e.cfg.ClockNowMs(), "policy.override", slotID, "", "", fmt.Sprintf("%s[%s] -> %s", methodID, actor, policy))
		return struct{}{}, nil
	})
	if gErr == nil {
		e.NotifyDashboard()
	}
	return gErr
}

func (e *Engine) RevokeAutoJoinScope(scopeDigest string, closeMatchingSessions bool) {
	var cancels []cancelTarget
	var closedIDs []string
	_, _ = store.Atomic(e.persistence, func(d *store.HubStoreData) (struct{}, *model.G2AError) {
		delete(d.ApprovedScopes, scopeDigest)
		if closeMatchingSessions {
			for _, sess := range d.Sessions {
				if sess.ScopeDigest == scopeDigest && sess.State != "closed" {
					cancels = append(cancels, e.cancelAndFenceSessionQueuesInTx(d, sess)...)
					sess.State = "closed"
					closedIDs = append(closedIDs, sess.SessionID)
				}
			}
		}
		if len(closedIDs) > 0 {
			d.PruneClosedSessions()
		}
		d.AppendAudit(e.cfg.ClockNowMs(), "scope.auto_join_revoked", "", "", "", scopeDigest)
		return struct{}{}, nil
	})
	e.broadcastWakeup()
	for _, c := range cancels {
		_, _ = e.originClient.Execute(c.targetURL, model.OriginExecuteRequest{
			SlotID: c.slotID, Epoch: c.epoch, Kind: "cancel", ExecutionID: c.execID, Args: map[string]any{},
		})
	}
	for _, sid := range closedIDs {
		e.pushStream.PushEvent(sid, "session.closed", map[string]any{"reason": "scope_revoked"})
	}
}

func (e *Engine) RevokeSessionFromTrustedUI(sessionID string) *model.G2AError {
	var cancels []cancelTarget
	_, gErr := store.Atomic(e.persistence, func(d *store.HubStoreData) (struct{}, *model.G2AError) {
		sess, ok := d.Sessions[sessionID]
		if !ok {
			return struct{}{}, model.NewError(model.ErrNotFound, 404, "Unknown session_id")
		}
		if sess.State != "closed" {
			cancels = e.cancelAndFenceSessionQueuesInTx(d, sess)
			sess.State = "closed"
			d.PruneClosedSessions()
			d.AppendAudit(e.cfg.ClockNowMs(), "session.revoked_by_player", sess.Scope.SlotID, sessionID, "", "Revoked from Trusted UI")
		}
		return struct{}{}, nil
	})
	if gErr != nil {
		return gErr
	}
	e.broadcastWakeup()
	for _, c := range cancels {
		_, _ = e.originClient.Execute(c.targetURL, model.OriginExecuteRequest{
			SlotID: c.slotID, Epoch: c.epoch, Kind: "cancel", ExecutionID: c.execID, Args: map[string]any{},
		})
	}
	e.pushStream.PushEvent(sessionID, "session.closed", map[string]any{"reason": "revoked_by_player"})
	return nil
}

func (e *Engine) ToggleSessionModality(sessionID string, allowVisual, allowAudio bool) *model.G2AError {
	_, gErr := store.Atomic(e.persistence, func(d *store.HubStoreData) (struct{}, *model.G2AError) {
		sess, ok := d.Sessions[sessionID]
		if !ok {
			return struct{}{}, model.NewError(model.ErrNotFound, 404, "Unknown session_id")
		}
		sess.Scope.AllowVisual = allowVisual
		sess.Scope.AllowAudio = allowAudio
		sess.IdleBackoffMs = e.cfg.MinSyncIntervalMs
		sess.NextAllowedPullMs = 0
		d.AppendAudit(e.cfg.ClockNowMs(), "session.modality_toggled", sess.Scope.SlotID, sessionID, "",
			fmt.Sprintf("visual=%v audio=%v", allowVisual, allowAudio))
		return struct{}{}, nil
	})
	if gErr == nil {
		e.NotifyDashboard()
	}
	return gErr
}

func (e *Engine) CheckSessionRateLimit(sessionID string) *model.G2AError {
	if e.cfg.RateLimitPerWindow <= 0 || e.cfg.RateLimitWindowMs <= 0 {
		return nil
	}
	_, gErr := store.MutateVolatile(e.persistence, func(d *store.HubStoreData) (struct{}, *model.G2AError) {
		sess, ok := d.Sessions[sessionID]
		if !ok {
			return struct{}{}, nil
		}
		now := e.cfg.ClockNowMs()
		if now-sess.RateWindowStartMs >= e.cfg.RateLimitWindowMs {
			sess.RateWindowStartMs = now
			sess.RateWindowCount = 1
			return struct{}{}, nil
		}
		if sess.RateWindowCount >= e.cfg.RateLimitPerWindow {
			wait := e.cfg.RateLimitWindowMs - (now - sess.RateWindowStartMs)
			if wait < 50 {
				wait = 50
			}
			rlErr := model.NewError(model.ErrResourceLimit, 429, "Session request rate limit exceeded")
			rlErr.RetryAfterMs = &wait
			return struct{}{}, rlErr
		}
		sess.RateWindowCount++
		return struct{}{}, nil
	})
	return gErr
}

// ReapExpiredConfirmationsOnce enforces Section 5.2 Item 2: unanswered confirmation prompts upon timeout
// are treated as declined, cancelling the queue and activating the target refusal cooldown.
func (e *Engine) ReapExpiredConfirmationsOnce() int {
	if e.cfg.ConfirmPromptTimeoutMs <= 0 {
		return 0
	}
	type expiredItem struct {
		sessionID string
		queueID   string
	}
	expired, _ := store.MutateVolatile(e.persistence, func(d *store.HubStoreData) ([]expiredItem, *model.G2AError) {
		now := e.cfg.ClockNowMs()
		var out []expiredItem
		reason := "confirmation_timed_out"
		for _, rec := range d.Queues {
			if rec.State == "awaiting_confirmation" && rec.ConfirmDeadlineMs > 0 && now >= rec.ConfirmDeadlineMs {
				rec.State = "cancelled"
				for i := range rec.StepReceipts {
					if rec.StepReceipts[i].State == "pending" {
						rec.StepReceipts[i].State = "cancelled"
						rCopy := reason
						rec.StepReceipts[i].FailureReason = &rCopy
					}
				}
				if rec.Fingerprint != "" {
					d.ConfirmCooldowns[rec.SessionID+"|"+rec.Fingerprint] = now + e.cfg.DefaultConfirmCooldownMs
				}
				slotID := ""
				if sess := d.Sessions[rec.SessionID]; sess != nil {
					slotID = sess.Scope.SlotID
				}
				d.AppendAudit(now, "queue.confirm_timeout", slotID, rec.SessionID, rec.QueueID, "Unanswered confirmation prompt timed out into cooldown")
				out = append(out, expiredItem{sessionID: rec.SessionID, queueID: rec.QueueID})
			}
		}
		if len(out) > 0 {
			d.PruneTerminalQueues()
		}
		return out, nil
	})
	for _, item := range expired {
		e.consent.PopQueuePrompt(item.sessionID, item.queueID)
		e.pushStream.PushEvent(item.sessionID, "queue.declined", map[string]any{
			"queue_id":    item.queueID,
			"cooldown_ms": e.cfg.DefaultConfirmCooldownMs,
			"reason":      "confirmation_timed_out",
		})
	}
	if len(expired) > 0 {
		e.NotifyDashboard()
	}
	return len(expired)
}

func (e *Engine) ReapExpiredSessionLeasesOnce() int {
	e.ReapExpiredConfirmationsOnce()
	reaped, _ := store.MutateVolatile(e.persistence, func(d *store.HubStoreData) (int, *model.G2AError) {
		now := e.cfg.ClockNowMs()
		count := 0
		for _, sess := range d.Sessions {
			if sess.State == "active" && now >= sess.LeaseDeadlineMs {
				sess.State = "disconnected_grace"
				sess.LocalFallbackActivated = true
				count++
				d.AppendAudit(now, "session.lease_expired", sess.Scope.SlotID, sess.SessionID, "", "Transitioned to disconnected_grace")
			}
		}
		return count, nil
	})
	if reaped > 0 {
		e.broadcastWakeup()
	}
	return reaped
}

func (e *Engine) StartBackgroundLeaseReaper(stopCh <-chan struct{}, interval time.Duration) {
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-stopCh:
				return
			case <-ticker.C:
				e.ReapExpiredSessionLeasesOnce()
			}
		}
	}()
}

func (e *Engine) SnapshotDashboardStatus() map[string]any {
	res, _ := store.ReadOnly(e.persistence, func(d *store.HubStoreData) (map[string]any, *model.G2AError) {
		slotKeys := make([]string, 0, len(d.Slots))
		for k := range d.Slots {
			slotKeys = append(slotKeys, k)
		}
		sort.Strings(slotKeys)
		slotsOut := make([]map[string]any, 0, len(slotKeys))
		for _, k := range slotKeys {
			sl := d.Slots[k]
			v := d.SlotViews[sl.SlotID]
			var ep, rev int64
			if v != nil {
				ep, rev = v.Epoch, v.Rev
			}
			capsCopy := make([]model.CapabilityEntry, len(sl.Capabilities))
			copy(capsCopy, sl.Capabilities)
			slotsOut = append(slotsOut, map[string]any{
				"slot_id":                sl.SlotID,
				"role_kind":              sl.RoleKind,
				"billing_mode":           sl.BillingMode,
				"player_id":              sl.PlayerID,
				"player_online":          sl.PlayerOnline,
				"disabled":               sl.Disabled,
				"shared_cooldown_ms":     sl.SharedCooldownMs,
				"allowlist_criteria_url": sl.AllowlistCriteriaURL,
				"epoch":                  ep,
				"rev":                    rev,
				"capabilities":           len(capsCopy),
				"capability_items":       capsCopy,
			})
		}

		sessKeys := make([]string, 0, len(d.Sessions))
		for k := range d.Sessions {
			sessKeys = append(sessKeys, k)
		}
		sort.Strings(sessKeys)
		sessOut := make([]map[string]any, 0, len(sessKeys))
		for _, k := range sessKeys {
			se := d.Sessions[k]
			sessOut = append(sessOut, map[string]any{
				"session_id":       se.SessionID,
				"core_id":          se.CallerCoreID,
				"slot_id":          se.Scope.SlotID,
				"state":            se.State,
				"allow_visual":     se.Scope.AllowVisual,
				"allow_audio":      se.Scope.AllowAudio,
				"consumption_used": se.ConsumptionUsed,
				"consumption_cap":  se.ConsumptionCap,
			})
		}

		autoScopes := make([]string, 0, len(d.ApprovedScopes))
		for sd := range d.ApprovedScopes {
			autoScopes = append(autoScopes, sd)
		}
		sort.Strings(autoScopes)

		overridesCopy := make(map[string]string, len(d.PolicyOverrides))
		for pk, pv := range d.PolicyOverrides {
			overridesCopy[pk] = pv
		}

		auditCopy := make([]store.AuditEntry, len(d.AuditLog))
		copy(auditCopy, d.AuditLog)
		return map[string]any{
			"g2a_version":      model.WireVersion,
			"slots":            slotsOut,
			"sessions":         sessOut,
			"auto_join_scopes": autoScopes,
			"policy_overrides": overridesCopy,
			"audit_log":        auditCopy,
			"pending":          e.consent.SnapshotPending(),
			"core_directory": map[string]any{
				"custom_endpoint_open": true,
				"featured": []map[string]string{
					{"name": "Mot Reference Core (Local Ollama / OpenAI)", "mode": "Local or Cloud", "note": "Open-source reference Computation Core with SQLite portable memory"},
					{"name": "Custom G2A-Compatible Endpoint", "mode": "BYO Core", "note": "Connect any third-party cloud or local model implementing G2A V0.0.1"},
				},
			},
		}, nil
	})
	return res
}

// --- Internal Helpers ---

func (e *Engine) projectContextInTx(d *store.HubStoreData, sess *store.SessionRecord, playerCommand *string) model.ProjectedContext {
	view, ok := d.SlotViews[sess.Scope.SlotID]
	if !ok {
		return model.ProjectedContext{
			SlotID:                sess.Scope.SlotID,
			Epoch:                 1,
			Rev:                   0,
			CapabilityTableDigest: sess.Scope.CapabilityTableDigest,
			PlayerCommand:         playerCommand,
			StateBrief:            map[string]any{},
			ActiveMethods:         []model.ActiveMethodGrant{},
		}
	}
	keys := make([]string, 0, len(view.Methods))
	for k := range view.Methods {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	projected := make([]model.ActiveMethodGrant, 0)
	for _, k := range keys {
		g := view.Methods[k]
		allowed := sess.Scope.PermittedMethods[g.MethodID]
		if containsString(allowed, g.Actor) && (g.Readiness == "ready" || g.Readiness == "staged") {
			projected = append(projected, g)
		}
	}
	ctx := model.ProjectedContext{
		SlotID:                sess.Scope.SlotID,
		Epoch:                 view.Epoch,
		Rev:                   view.Rev,
		CapabilityTableDigest: sess.Scope.CapabilityTableDigest,
		PlayerCommand:         playerCommand,
		StateBrief:            view.StateBrief,
		ActiveMethods:         projected,
	}
	if sess.Scope.AllowVisual {
		ctx.VisualRef = view.LatestVisual
	}
	if sess.Scope.AllowAudio {
		ctx.AudioRef = view.LatestAudio
	}
	if containsString(sess.NegotiatedExtensions, "org.g2a.narrative.timeline.v1") && len(view.AllowedTimelineTags) > 0 {
		ctx.AllowedTimelineTags = append([]string(nil), view.AllowedTimelineTags...)
	}
	if len(sess.NegotiatedExtensions) > 0 && len(view.ExtensionsData) > 0 {
		extProj := make(map[string]map[string]any)
		for _, extName := range sess.NegotiatedExtensions {
			if extMap, hasExt := view.ExtensionsData[extName]; hasExt && extMap != nil {
				cp := make(map[string]any, len(extMap))
				for mk, mv := range extMap {
					cp[mk] = mv
				}
				extProj[extName] = cp
			}
		}
		if len(extProj) > 0 {
			ctx.ExtensionsData = extProj
		}
	}
	return ctx
}

func (e *Engine) toReceiptInTx(d *store.HubStoreData, sess *store.SessionRecord, rec *store.QueueLedgerRecord) model.QueueReceipt {
	steps := make([]model.StepReceipt, len(rec.StepReceipts))
	copy(steps, rec.StepReceipts)
	var latest *model.ProjectedContext
	if sess.PlayerOnline {
		ctx := e.projectContextInTx(d, sess, nil)
		latest = &ctx
	}
	return model.QueueReceipt{
		QueueID:       rec.QueueID,
		QueueState:    rec.State,
		StepReceipts:  steps,
		LatestContext: latest,
	}
}

func (e *Engine) abortObsoleteEpochQueuesInTx(d *store.HubStoreData, slotID string, newEpoch int64) {
	reason := "epoch_advanced"
	for _, rec := range d.Queues {
		sess := d.Sessions[rec.SessionID]
		if sess != nil && sess.Scope.SlotID == slotID && rec.Queue.Epoch < newEpoch {
			if rec.State == "running" || rec.State == "awaiting_confirmation" {
				rec.State = "aborted"
				rec.CurrentExecutionID = nil
				for i := range rec.StepReceipts {
					if rec.StepReceipts[i].State == "pending" || rec.StepReceipts[i].State == "executing" {
						rec.StepReceipts[i].State = "aborted"
						rec.StepReceipts[i].FailureReason = &reason
					}
				}
			}
		}
	}
}

func (e *Engine) clearUndispatchedQueuesInTx(d *store.HubStoreData, sess *store.SessionRecord) {
	reason := "player_offline"
	for _, rec := range d.Queues {
		if rec.SessionID == sess.SessionID && (rec.State == "awaiting_confirmation" || rec.State == "running") {
			rec.State = "aborted"
			rec.CurrentExecutionID = nil
			for i := range rec.StepReceipts {
				if rec.StepReceipts[i].State == "pending" {
					rec.StepReceipts[i].State = "aborted"
					rec.StepReceipts[i].FailureReason = &reason
				}
			}
		}
	}
	sess.ActiveQueueID = nil
}

func (e *Engine) cancelAndFenceSessionQueuesInTx(d *store.HubStoreData, sess *store.SessionRecord) []cancelTarget {
	var cancels []cancelTarget
	slot := d.Slots[sess.Scope.SlotID]
	view := d.SlotViews[sess.Scope.SlotID]
	for _, rec := range d.Queues {
		if rec.SessionID == sess.SessionID && (rec.State == "awaiting_confirmation" || rec.State == "running") {
			if rec.CurrentExecutionID != nil && slot != nil && view != nil {
				cancels = append(cancels, cancelTarget{
					targetURL: slot.OriginExecuteURL,
					slotID:    sess.Scope.SlotID,
					epoch:     view.Epoch,
					execID:    *rec.CurrentExecutionID,
				})
				rec.CurrentExecutionID = nil
			}
			rec.State = "cancelled"
			for i := range rec.StepReceipts {
				if rec.StepReceipts[i].State == "pending" || rec.StepReceipts[i].State == "executing" {
					rec.StepReceipts[i].State = "cancelled"
				}
			}
		}
	}
	sess.ActiveQueueID = nil
	return cancels
}

func intersectScopeWithCapabilities(req model.RequestedScopeSpec, slot *store.SlotRecord, coreID string) model.FrozenScope {
	permitted := make(map[string][]string)
	for _, capDef := range slot.Capabilities {
		reqActors, hasExplicit := req.PermittedMethods[capDef.MethodID]
		if len(req.PermittedMethods) > 0 && !hasExplicit {
			continue
		}
		var chosen []string
		if len(req.PermittedMethods) == 0 {
			chosen = append(chosen, capDef.PermittedActors...)
		} else {
			for _, a := range reqActors {
				if containsString(capDef.PermittedActors, a) {
					chosen = append(chosen, a)
				}
			}
		}
		if slot.RoleKind == "independent_npc" {
			filtered := make([]string, 0, len(chosen))
			for _, a := range chosen {
				if a != "player" {
					filtered = append(filtered, a)
				}
			}
			chosen = filtered
		}
		if len(chosen) > 0 {
			sort.Strings(chosen)
			permitted[capDef.MethodID] = chosen
		}
	}
	return model.FrozenScope{
		PlayerID:              slot.PlayerID,
		CoreID:                coreID,
		GameInstanceID:        slot.GameInstanceID,
		SlotID:                slot.SlotID,
		RoleKind:              slot.RoleKind,
		BillingMode:           slot.BillingMode,
		PermittedMethods:      permitted,
		CapabilityTableDigest: slot.CapabilityTableDigest,
		AllowVisual:           req.AllowVisual,
		AllowAudio:            req.AllowAudio,
		AllowChat:             req.AllowChat,
	}
}

func containsString(list []string, target string) bool {
	for _, item := range list {
		if item == target {
			return true
		}
	}
	return false
}

func generateID(prefix string) string {
	var b [6]byte
	_, _ = rand.Read(b[:])
	return fmt.Sprintf("%s-%s", prefix, hex.EncodeToString(b[:]))
}
