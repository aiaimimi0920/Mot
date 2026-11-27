package g2acore

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha1"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	wsMagicGUID     = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
	maxSeenEventIDs = 1024
)

type ConnectOptions struct {
	PlayerID            string
	FactionID           string
	ConsumptionCap      *int64
	RequestedScope      map[string]any
	Extensions          map[string]any
	SkipBackgroundLoops bool
}

type CoreMetrics struct {
	ContextsReceived  int64
	QueuesSubmitted   int64
	StreamReconnects  int64
	BackoffShieldHits int64
}

type InvokeOptions struct {
	Actor                    string
	DeicticHint              string
	IntentSummary            string
	Trigger                  string
	DisableAutoPrerequisites bool
	NoWaitTerminal           bool
}

// CoreSDK implements the Tier 3 Computation Core state machine (G2A V0.0.1).
type CoreSDK struct {
	Planner               CognitivePlanner
	Memory                *InMemoryCompanionStore
	AllowLoopback         bool
	StreamMode            string // "websocket" (default) | "sse"
	OnContext             func(ProjectedContext)
	OnCapabilitiesUpdated func([]CapabilityEntry)
	OnSessionClosed       func()

	mu                   sync.Mutex
	hubEndpoint          string
	authToken            string
	sessionID            string
	frozenScope          *FrozenScope
	negotiatedExtensions []string
	latestContext        *ProjectedContext
	metrics              CoreMetrics
	actionsPaused        bool
	closed              bool
	nextAllowedPullMs   int64
	lastEventID         string
	seenOrder           []string
	seenSet             map[string]struct{}
	slotDigests         map[string]string
	capabilityCache     map[string][]CapabilityEntry
	pendingConfirm      map[string]MethodCallQueue
	confirmHolds        map[string]struct{}
	earlyConfirmed      map[string]struct{}
	earlyDeclined       map[string]int64
	declinedQueues      map[string]struct{}
	confirmedRecs       map[string]*QueueReceipt
	refusalCooldowns    map[string]int64
	streamReady         chan struct{}
	cancelBg            context.CancelFunc
	httpClient          *http.Client
	CustomPostTransport func(ctx context.Context, op string, payload any, sessionID string) (json.RawMessage, error)
}

func NewCoreSDK(planner CognitivePlanner, allowLoopback bool) *CoreSDK {
	if planner == nil {
		planner = NoOpPlanner{}
	}
	return &CoreSDK{
		Planner:          planner,
		AllowLoopback:    allowLoopback,
		StreamMode:       "websocket",
		seenSet:          make(map[string]struct{}),
		slotDigests:      make(map[string]string),
		capabilityCache:  make(map[string][]CapabilityEntry),
		pendingConfirm:   make(map[string]MethodCallQueue),
		confirmHolds:     make(map[string]struct{}),
		earlyConfirmed:   make(map[string]struct{}),
		earlyDeclined:    make(map[string]int64),
		declinedQueues:   make(map[string]struct{}),
		confirmedRecs:    make(map[string]*QueueReceipt),
		refusalCooldowns: make(map[string]int64),
		streamReady:      make(chan struct{}),
		httpClient: &http.Client{
			Timeout: 12 * time.Second,
			Transport: &http.Transport{
				Proxy:               nil,
				MaxIdleConnsPerHost: 8,
				TLSClientConfig:     &tls.Config{MinVersion: tls.VersionTLS13},
			},
		},
	}
}

func (c *CoreSDK) SessionID() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.sessionID
}

func (c *CoreSDK) Scope() *FrozenScope {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.frozenScope == nil {
		return nil
	}
	cp := *c.frozenScope
	return &cp
}

func (c *CoreSDK) NegotiatedExtensions() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]string, len(c.negotiatedExtensions))
	copy(out, c.negotiatedExtensions)
	return out
}

func (c *CoreSDK) LatestContext() *ProjectedContext {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.latestContext == nil {
		return nil
	}
	cp := *c.latestContext
	return &cp
}

func (c *CoreSDK) Metrics() CoreMetrics {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.metrics
}

func (c *CoreSDK) CapabilityCacheCount(digest string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.capabilityCache[digest])
}

func (c *CoreSDK) SlotDigest(slotID string) string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.slotDigests[slotID]
}

func (c *CoreSDK) GetCachedCapabilities(slotID string) []CapabilityEntry {
	c.mu.Lock()
	defer c.mu.Unlock()
	if slotID == "" && c.frozenScope != nil {
		slotID = c.frozenScope.SlotID
	}
	digest := c.slotDigests[slotID]
	if digest == "" && c.frozenScope != nil {
		digest = c.frozenScope.CapabilityTableDigest
	}
	src := c.capabilityCache[digest]
	out := make([]CapabilityEntry, len(src))
	copy(out, src)
	return out
}

func (c *CoreSDK) SendHeartbeat(ctx context.Context) (int64, error) {
	raw, err := c.postEnvelope(ctx, "session.heartbeat", map[string]any{}, c.SessionID(), nil)
	if err != nil {
		return 0, err
	}
	var res struct {
		LeaseDeadlineMs int64 `json:"lease_deadline_ms"`
	}
	_ = json.Unmarshal(raw, &res)
	return res.LeaseDeadlineMs, nil
}

func (c *CoreSDK) ResumeSession(ctx context.Context) (bool, error) {
	raw, err := c.postEnvelope(ctx, "session.resume", map[string]any{}, c.SessionID(), nil)
	if err != nil {
		return false, err
	}
	var res struct {
		ActionsPaused bool `json:"actions_paused"`
	}
	_ = json.Unmarshal(raw, &res)
	c.mu.Lock()
	c.actionsPaused = res.ActionsPaused
	c.mu.Unlock()
	return res.ActionsPaused, nil
}

func (c *CoreSDK) Connect(
	ctx context.Context,
	hubEndpoint string,
	authToken string,
	slotID string,
	opts ConnectOptions,
) (*FrozenScope, error) {
	if err := ValidateOutboundURLAgainstSSRF(hubEndpoint, c.AllowLoopback); err != nil {
		return nil, err
	}
	base := strings.TrimRight(hubEndpoint, "/")

	c.mu.Lock()
	cachedDigest := c.slotDigests[slotID]
	c.hubEndpoint = base
	c.authToken = authToken
	c.closed = false
	c.streamReady = make(chan struct{})
	c.mu.Unlock()

	reqScope := opts.RequestedScope
	if reqScope == nil {
		reqScope = map[string]any{"allow_chat": true}
	}
	authPayload := map[string]any{
		"slot_id":         slotID,
		"requested_scope": reqScope,
	}
	if opts.PlayerID != "" {
		authPayload["player_id"] = opts.PlayerID
	}
	if opts.FactionID != "" {
		authPayload["faction_id"] = opts.FactionID
	}
	if cachedDigest != "" {
		authPayload["cached_capability_digest"] = cachedDigest
	}
	if opts.ConsumptionCap != nil {
		authPayload["consumption_cap"] = *opts.ConsumptionCap
	}

	rawRes, err := c.postEnvelope(ctx, "session.authorize", authPayload, "", opts.Extensions)
	if err != nil {
		return nil, err
	}
	var authRes struct {
		SessionID             string            `json:"session_id"`
		CapabilityTableDigest string            `json:"capability_table_digest"`
		CapabilityTable       []CapabilityEntry `json:"capability_table"`
		FrozenScope           FrozenScope       `json:"frozen_scope"`
		NegotiatedExtensions  []string          `json:"negotiated_extensions"`
	}
	if err := UnmarshalStrict(rawRes, &authRes); err != nil {
		return nil, err
	}
	if authRes.CapabilityTable != nil {
		verified, err := DigestSHA256("g2a.cap.v1", authRes.CapabilityTable)
		if err != nil || verified != authRes.CapabilityTableDigest {
			return nil, &G2AError{Code: "invalid_arguments", HTTPStatus: 400, Message: "capability_table_digest mismatch"}
		}
		c.mu.Lock()
		c.capabilityCache[authRes.CapabilityTableDigest] = authRes.CapabilityTable
		c.mu.Unlock()
	}

	bgCtx, cancel := context.WithCancel(context.Background())
	c.mu.Lock()
	c.sessionID = authRes.SessionID
	c.frozenScope = &authRes.FrozenScope
	c.negotiatedExtensions = authRes.NegotiatedExtensions
	c.slotDigests[slotID] = authRes.CapabilityTableDigest
	c.cancelBg = cancel
	c.mu.Unlock()

	if !opts.SkipBackgroundLoops {
		go c.heartbeatLoop(bgCtx)
		if c.StreamMode == "sse" {
			go c.sseStreamLoop(bgCtx)
		} else {
			go c.wsStreamLoop(bgCtx)
		}
		select {
		case <-c.streamReady:
		case <-time.After(1500 * time.Millisecond):
		case <-ctx.Done():
		}
	}
	return &authRes.FrozenScope, nil
}

// ConnectQuick performs one-step Hub auto-discovery (with optional fallbackURL) and opens a ready CoreSDK session.
func ConnectQuick(
	ctx context.Context,
	slotID string,
	authToken string,
	planner CognitivePlanner,
	hubEndpoint string,
	fallbackURL string,
	opts ConnectOptions,
) (*CoreSDK, error) {
	resolved := hubEndpoint
	if hubEndpoint == "" || hubEndpoint == "auto" {
		u, err := DiscoverLocalHubCoreURL("")
		if err != nil {
			if fallbackURL == "" {
				return nil, err
			}
			resolved = fallbackURL
		} else {
			resolved = u
		}
	}
	sdk := NewCoreSDK(planner, true)
	if _, err := sdk.Connect(ctx, resolved, authToken, slotID, opts); err != nil {
		return nil, err
	}
	return sdk, nil
}

type QueueBuilder struct {
	sdk           *CoreSDK
	ctx           ProjectedContext
	intentSummary string
	trigger       string
	supersedes    bool
	steps         []QueueStep
	grants        map[string]ActiveMethodGrant
	caps          map[string]CapabilityEntry
}

func (c *CoreSDK) NewQueueBuilder(pc ProjectedContext, intentSummary string) *QueueBuilder {
	isCmd := pc.PlayerCommand != nil && *pc.PlayerCommand != ""
	trig := "proactive"
	if isCmd {
		trig = "player_command"
	}
	grants := make(map[string]ActiveMethodGrant, len(pc.ActiveMethods))
	for _, g := range pc.ActiveMethods {
		grants[g.MethodID+"|"+g.Actor] = g
	}
	c.mu.Lock()
	rawCaps := c.capabilityCache[pc.CapabilityTableDigest]
	c.mu.Unlock()
	capMap := make(map[string]CapabilityEntry, len(rawCaps))
	for _, capDef := range rawCaps {
		capMap[capDef.MethodID] = capDef
	}
	return &QueueBuilder{
		sdk:           c,
		ctx:           pc,
		intentSummary: intentSummary,
		trigger:       trig,
		supersedes:    isCmd,
		grants:        grants,
		caps:          capMap,
	}
}

func (b *QueueBuilder) AddStep(methodID, actor string, args map[string]any, validate bool) (*QueueBuilder, error) {
	if args == nil {
		args = map[string]any{}
	}
	idx := len(b.steps)
	if validate {
		b.sdk.mu.Lock()
		scope := b.sdk.frozenScope
		b.sdk.mu.Unlock()
		if scope != nil && scope.PermittedMethods != nil {
			if allowed, exists := scope.PermittedMethods[methodID]; exists {
				okActor := false
				for _, a := range allowed {
					if a == actor {
						okActor = true
						break
					}
				}
				if !okActor {
					return nil, &G2AError{Code: "permission_denied", HTTPStatus: 403, Message: fmt.Sprintf("scope forbids (%s, %s)", methodID, actor)}
				}
			}
		}
		grant, ok := b.grants[methodID+"|"+actor]
		if !ok {
			return nil, &G2AError{Code: "invalid_arguments", HTTPStatus: 400, Message: fmt.Sprintf("method (%s, %s) not active", methodID, actor)}
		}
		if idx == 0 && grant.Readiness != "ready" {
			return nil, &G2AError{Code: "stale_context", HTTPStatus: 409, Message: "initial step is staged"}
		}
		if tid, hasTID := args["target_id"].(string); hasTID && len(grant.Targets) > 0 {
			found := false
			for _, t := range grant.Targets {
				if t.TargetID == tid {
					found = true
					if idx == 0 && t.Readiness != "ready" {
						return nil, &G2AError{Code: "stale_context", HTTPStatus: 409, Message: "initial target is staged"}
					}
					break
				}
			}
			if !found {
				return nil, &G2AError{Code: "not_found", HTTPStatus: 404, Message: "target_id not in active candidates"}
			}
		}
		if capDef, hasCap := b.caps[methodID]; hasCap && len(capDef.ParamsSchema) > 0 {
			if !validateSchemaObject(args, capDef.ParamsSchema, 0) {
				return nil, &G2AError{Code: "invalid_arguments", HTTPStatus: 400, Message: "step args failed local JSON Schema check"}
			}
		}
	}
	b.steps = append(b.steps, QueueStep{StepIndex: idx, MethodID: methodID, Actor: actor, Args: args})
	return b, nil
}

func (b *QueueBuilder) AddStepAuto(methodID, actor string, args map[string]any, validate bool) (*QueueBuilder, error) {
	caps := make([]CapabilityEntry, 0, len(b.caps))
	for _, c := range b.caps {
		caps = append(caps, c)
	}
	chain := ResolvePrerequisiteSteps(methodID, actor, args, b.ctx, caps)
	for _, st := range chain {
		if _, err := b.AddStep(st.MethodID, st.Actor, st.Args, validate); err != nil {
			return nil, err
		}
	}
	return b, nil
}

func (b *QueueBuilder) Submit(ctx context.Context, queueID string) (*QueueReceipt, error) {
	if len(b.steps) == 0 {
		return nil, &G2AError{Code: "invalid_arguments", HTTPStatus: 400, Message: "cannot submit empty queue"}
	}
	if queueID == "" {
		queueID = "Q-" + randomHex(6)
	}
	return b.sdk.SubmitQueueWithRecovery(ctx, MethodCallQueue{
		QueueID:       queueID,
		Epoch:         b.ctx.Epoch,
		BasedOnRev:    b.ctx.Rev,
		Trigger:       b.trigger,
		Supersedes:    b.supersedes,
		IntentSummary: b.intentSummary,
		Steps:         b.steps,
	})
}

func (b *QueueBuilder) SubmitAndWait(ctx context.Context, queueID string) (*QueueReceipt, error) {
	if queueID == "" {
		queueID = "Q-" + randomHex(6)
	}
	initial, err := b.Submit(ctx, queueID)
	if err != nil {
		return nil, err
	}
	return b.sdk.WaitForQueueTerminal(ctx, queueID, initial)
}

func (c *CoreSDK) InvokeCapability(
	ctx context.Context,
	methodIDOrName string,
	args map[string]any,
	opts InvokeOptions,
) (*QueueReceipt, error) {
	pc, err := c.PullContextIfDue(ctx)
	if err != nil {
		return nil, err
	}
	if pc == nil {
		pc = c.LatestContext()
	}
	if pc == nil {
		pc, err = c.PullContext(ctx)
		if err != nil {
			return nil, err
		}
	}
	caps := c.GetCachedCapabilities(pc.SlotID)
	resolvedMid := methodIDOrName
	var matchedCap *CapabilityEntry
	for i := range caps {
		if caps[i].MethodID == methodIDOrName {
			matchedCap = &caps[i]
			break
		}
	}
	if matchedCap == nil {
		low := strings.ToLower(strings.TrimSpace(methodIDOrName))
		for i := range caps {
			if strings.ToLower(caps[i].Name) == low {
				matchedCap = &caps[i]
				resolvedMid = caps[i].MethodID
				break
			}
		}
	}
	scope := c.Scope()
	var allowedActors []string
	hasScopeEntry := false
	if scope != nil && scope.PermittedMethods != nil {
		allowedActors, hasScopeEntry = scope.PermittedMethods[resolvedMid]
	}
	actorAllowed := func(a string) bool {
		if !hasScopeEntry {
			return true
		}
		for _, okA := range allowedActors {
			if okA == a {
				return true
			}
		}
		return false
	}

	resolvedActor := opts.Actor
	if resolvedActor == "" {
		for _, g := range pc.ActiveMethods {
			if g.MethodID == resolvedMid && g.Actor == "companion" && actorAllowed("companion") {
				resolvedActor = "companion"
				break
			}
		}
		if resolvedActor == "" {
			for _, g := range pc.ActiveMethods {
				if g.MethodID == resolvedMid && actorAllowed(g.Actor) {
					resolvedActor = g.Actor
					break
				}
			}
		}
		if resolvedActor == "" {
			resolvedActor = "companion"
		}
	}

	stepArgs := map[string]any{}
	for k, v := range args {
		stepArgs[k] = v
	}
	if opts.DeicticHint != "" {
		if _, hasTID := stepArgs["target_id"]; !hasTID {
			for _, g := range pc.ActiveMethods {
				if g.MethodID == resolvedMid && g.Actor == resolvedActor && len(g.Targets) > 0 {
					if cand := ResolveDeicticTarget(opts.DeicticHint, g.Targets); cand != nil {
						stepArgs["target_id"] = cand.TargetID
					}
					break
				}
			}
		}
	}

	summary := opts.IntentSummary
	if summary == "" {
		if matchedCap != nil {
			summary = "Invoke " + matchedCap.Name
		} else {
			summary = "Invoke " + resolvedMid
		}
	}
	builder := c.NewQueueBuilder(*pc, summary)
	if opts.Trigger != "" {
		builder.trigger = opts.Trigger
		builder.supersedes = opts.Trigger == "player_command"
	}
	if !opts.DisableAutoPrerequisites {
		if _, err := builder.AddStepAuto(resolvedMid, resolvedActor, stepArgs, true); err != nil {
			return nil, err
		}
	} else {
		if _, err := builder.AddStep(resolvedMid, resolvedActor, stepArgs, true); err != nil {
			return nil, err
		}
	}
	if opts.NoWaitTerminal {
		rec, err := builder.Submit(ctx, "")
		if err != nil || rec != nil {
			return rec, err
		}
	} else {
		rec, err := builder.SubmitAndWait(ctx, "")
		if err != nil || rec != nil {
			return rec, err
		}
	}
	if fresh := c.LatestContext(); fresh != nil && (fresh.Epoch > pc.Epoch || fresh.Rev > pc.Rev) {
		retryBuilder := c.NewQueueBuilder(*fresh, summary)
		if opts.Trigger != "" {
			retryBuilder.trigger = opts.Trigger
			retryBuilder.supersedes = opts.Trigger == "player_command"
		}
		if !opts.DisableAutoPrerequisites {
			if _, err := retryBuilder.AddStepAuto(resolvedMid, resolvedActor, stepArgs, true); err != nil {
				return nil, err
			}
		} else {
			if _, err := retryBuilder.AddStep(resolvedMid, resolvedActor, stepArgs, true); err != nil {
				return nil, err
			}
		}
		if opts.NoWaitTerminal {
			return retryBuilder.Submit(ctx, "")
		}
		return retryBuilder.SubmitAndWait(ctx, "")
	}
	return nil, nil
}

func (c *CoreSDK) PullContext(ctx context.Context) (*ProjectedContext, error) {
	sid := c.SessionID()
	raw, err := c.postEnvelope(ctx, "context.pull", map[string]any{}, sid, nil)
	if err != nil {
		var gerr *G2AError
		if errors.As(err, &gerr) && gerr.Code == "resource_limit" && gerr.RetryAfterMs != nil {
			c.mu.Lock()
			c.nextAllowedPullMs = time.Now().UnixMilli() + *gerr.RetryAfterMs
			c.mu.Unlock()
		}
		return nil, err
	}
	var pc ProjectedContext
	if err := UnmarshalStrict(raw, &pc); err != nil {
		return nil, err
	}
	c.mu.Lock()
	c.latestContext = &pc
	c.mu.Unlock()
	return &pc, nil
}

func (c *CoreSDK) PullContextIfDue(ctx context.Context) (*ProjectedContext, error) {
	c.mu.Lock()
	if c.closed || c.sessionID == "" || time.Now().UnixMilli() < c.nextAllowedPullMs {
		if time.Now().UnixMilli() < c.nextAllowedPullMs {
			c.metrics.BackoffShieldHits++
		}
		c.mu.Unlock()
		return nil, nil
	}
	c.mu.Unlock()

	pc, err := c.PullContext(ctx)
	if err != nil {
		var gerr *G2AError
		if errors.As(err, &gerr) && gerr.Code == "resource_limit" {
			c.mu.Lock()
			c.metrics.BackoffShieldHits++
			c.mu.Unlock()
			return nil, nil
		}
		return nil, err
	}
	return pc, nil
}

func (c *CoreSDK) SubmitQueueWithRecovery(ctx context.Context, queue MethodCallQueue) (*QueueReceipt, error) {
	return c.submitQueueDepth(ctx, queue, 0)
}

func (c *CoreSDK) submitQueueDepth(ctx context.Context, queue MethodCallQueue, depth int) (*QueueReceipt, error) {
	now := time.Now().UnixMilli()
	var fp string
	if queue.Trigger == "proactive" {
		fp, _ = DigestSHA256("g2a.fp.v1", queue.Steps)
	}

	c.mu.Lock()
	if c.closed || c.sessionID == "" {
		c.mu.Unlock()
		return nil, nil
	}
	sid := c.sessionID
	for k, until := range c.refusalCooldowns {
		if now >= until {
			delete(c.refusalCooldowns, k)
		}
	}
	if queue.Trigger == "proactive" && fp != "" {
		if until, blocked := c.refusalCooldowns[fp]; blocked && now < until {
			c.mu.Unlock()
			return nil, nil
		}
	}
	c.mu.Unlock()

	c.mu.Lock()
	c.metrics.QueuesSubmitted++
	c.mu.Unlock()

	raw, err := c.postEnvelope(ctx, "queue.submit", queue, sid, nil)
	if err == nil {
		var rec QueueReceipt
		if uErr := UnmarshalStrict(raw, &rec); uErr != nil {
			return nil, uErr
		}
		return &rec, nil
	}

	var gerr *G2AError
	if !errors.As(err, &gerr) {
		return nil, err
	}
	switch gerr.Code {
	case "confirmation_required":
		c.mu.Lock()
		c.confirmHolds[queue.QueueID] = struct{}{}
		if _, fastConfirmed := c.earlyConfirmed[queue.QueueID]; fastConfirmed {
			delete(c.earlyConfirmed, queue.QueueID)
			c.mu.Unlock()
			return c.submitQueueDepth(ctx, queue, depth)
		}
		if cdMs, fastDeclined := c.earlyDeclined[queue.QueueID]; fastDeclined {
			delete(c.earlyDeclined, queue.QueueID)
			c.declinedQueues[queue.QueueID] = struct{}{}
			fpDecl, _ := DigestSHA256("g2a.fp.v1", queue.Steps)
			if fpDecl != "" {
				c.refusalCooldowns[fpDecl] = time.Now().UnixMilli() + cdMs
			}
			c.mu.Unlock()
			return nil, nil
		}
		c.pendingConfirm[queue.QueueID] = queue
		c.mu.Unlock()
		return nil, nil
	case "stale_context":
		if gerr.FreshContext != nil {
			c.mu.Lock()
			fc := *gerr.FreshContext
			c.latestContext = &fc
			c.mu.Unlock()
			if depth < 2 {
				return c.onContextReceived(ctx, *gerr.FreshContext, depth+1)
			}
		}
		return nil, nil
	case "outcome_unknown":
		return c.QueryQueue(ctx, queue.QueueID)
	default:
		return nil, err
	}
}

func (c *CoreSDK) QueryQueue(ctx context.Context, queueID string) (*QueueReceipt, error) {
	raw, err := c.postEnvelope(ctx, "queue.query", map[string]any{"queue_id": queueID}, c.SessionID(), nil)
	if err != nil {
		return nil, err
	}
	var rec QueueReceipt
	if err := UnmarshalStrict(raw, &rec); err != nil {
		return nil, err
	}
	return &rec, nil
}

func (c *CoreSDK) CancelQueue(ctx context.Context, queueID string) (*QueueReceipt, error) {
	raw, err := c.postEnvelope(ctx, "queue.cancel", map[string]any{"queue_id": queueID}, c.SessionID(), nil)
	if err != nil {
		return nil, err
	}
	var rec QueueReceipt
	if err := UnmarshalStrict(raw, &rec); err != nil {
		return nil, err
	}
	return &rec, nil
}

func (c *CoreSDK) SendChat(ctx context.Context, audience, text string) error {
	if audience == "" {
		audience = "player_only"
	}
	_, err := c.postEnvelope(ctx, "chat.send", map[string]any{
		"message_id": "msg-" + randomHex(6),
		"audience":   audience,
		"text":       text,
		"provenance": "ai_companion",
	}, c.SessionID(), nil)
	return err
}

func (c *CoreSDK) OnEventQueueConfirmed(ctx context.Context, queueID string) (*QueueReceipt, error) {
	c.mu.Lock()
	q, exists := c.pendingConfirm[queueID]
	if exists {
		delete(c.pendingConfirm, queueID)
		c.mu.Unlock()
		rec, err := c.SubmitQueueWithRecovery(ctx, q)
		if rec != nil {
			c.mu.Lock()
			c.confirmedRecs[queueID] = rec
			c.mu.Unlock()
		}
		return rec, err
	}
	c.earlyConfirmed[queueID] = struct{}{}
	c.mu.Unlock()
	return nil, nil
}

func (c *CoreSDK) OnEventQueueDeclined(queueID string, cooldownMs int64) {
	if cooldownMs <= 0 {
		cooldownMs = 60000
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.declinedQueues[queueID] = struct{}{}
	if q, exists := c.pendingConfirm[queueID]; exists {
		delete(c.pendingConfirm, queueID)
		if fp, err := DigestSHA256("g2a.fp.v1", q.Steps); err == nil && fp != "" {
			c.refusalCooldowns[fp] = time.Now().UnixMilli() + cooldownMs
		}
		return
	}
	c.earlyDeclined[queueID] = cooldownMs
}

func isNonTerminalQueueState(state string) bool {
	return state == "running" || state == "executing" || state == "awaiting_confirmation"
}

// WaitForQueueTerminal awaits terminal settlement of sustained ("running") steps or confirmation holds.
func (c *CoreSDK) WaitForQueueTerminal(ctx context.Context, queueID string, initialRec *QueueReceipt) (*QueueReceipt, error) {
	rec := initialRec
	if rec != nil && !isNonTerminalQueueState(rec.QueueState) {
		return rec, nil
	}
	for ctx.Err() == nil {
		c.mu.Lock()
		closed := c.closed
		_, everHeld := c.confirmHolds[queueID]
		_, declined := c.declinedQueues[queueID]
		confRec := c.confirmedRecs[queueID]
		c.mu.Unlock()
		if closed {
			return rec, nil
		}
		if declined {
			return &QueueReceipt{QueueID: queueID, QueueState: "cancelled"}, nil
		}
		if confRec != nil {
			if !isNonTerminalQueueState(confRec.QueueState) {
				return confRec, nil
			}
			rec = confRec
		}
		if initialRec == nil && !everHeld {
			return nil, nil
		}
		if everHeld && confRec == nil {
			time.Sleep(35 * time.Millisecond)
			continue
		}
		queried, err := c.QueryQueue(ctx, queueID)
		if err != nil {
			break
		}
		rec = queried
		if rec != nil && !isNonTerminalQueueState(rec.QueueState) {
			return rec, nil
		}
		time.Sleep(35 * time.Millisecond)
	}
	return rec, nil
}

func (c *CoreSDK) Shutdown(ctx context.Context, notifyHub bool) {
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return
	}
	c.closed = true
	sid := c.sessionID
	cancel := c.cancelBg
	c.mu.Unlock()

	if cancel != nil {
		cancel()
	}
	if notifyHub && sid != "" {
		_, _ = c.postEnvelope(ctx, "session.close", map[string]any{"reason": "client_shutdown"}, sid, nil)
	}
}

// ExecuteGoal runs a closed-loop multi-turn AI agent cycle with anti-repeat failure fingerprinting and verification.
func (c *CoreSDK) ExecuteGoal(
	ctx context.Context,
	objective string,
	successWhen func(ProjectedContext, *QueueReceipt) bool,
	maxTurns int,
) (*GoalExecutionReport, error) {
	if maxTurns <= 0 {
		maxTurns = 6
	}
	goalID := "goal-" + randomHex(5)
	failedFPs := make(map[string]struct{})
	var submitted []string
	var lastRec *QueueReceipt
	var lastCtx *ProjectedContext
	turns := 0

	for ctx.Err() == nil && turns < maxTurns {
		pulled, err := c.PullContextIfDue(ctx)
		if err != nil {
			return nil, err
		}
		if pulled == nil {
			pulled, err = c.PullContext(ctx)
			if err != nil {
				return nil, err
			}
		}
		lastCtx = pulled
		if successWhen != nil && successWhen(*pulled, lastRec) {
			return &GoalExecutionReport{
				GoalID: goalID, Objective: objective, Succeeded: true, Status: "completed",
				TurnsUsed: turns, SubmittedQueues: submitted, FinalContext: pulled,
			}, nil
		}

		synth := *pulled
		objCopy := objective
		synth.PlayerCommand = &objCopy
		caps := c.GetCachedCapabilities(synth.SlotID)
		dec, err := c.Planner.Plan(caps, synth, nil)
		if err != nil {
			return nil, err
		}
		if dec.ReplyText != "" {
			_ = c.SendChat(ctx, dec.Audience, dec.ReplyText)
		}

		filtered := make([]QueueStep, 0, len(dec.PlannedSteps))
		for _, s := range dec.PlannedSteps {
			fp, _ := DigestSHA256("g2a.agent.loop.v1", map[string]any{
				"brief": synth.StateBrief, "method_id": s.MethodID, "actor": s.Actor, "args": s.Args,
			})
			if _, seen := failedFPs[fp]; seen {
				break
			}
			filtered = append(filtered, QueueStep{StepIndex: len(filtered), MethodID: s.MethodID, Actor: s.Actor, Args: s.Args})
		}
		if len(filtered) == 0 {
			st := "no_executable_steps"
			if dec.GoalStatus == "blocked" {
				st = "blocked"
			}
			return &GoalExecutionReport{
				GoalID: goalID, Objective: objective, Succeeded: false, Status: st,
				TurnsUsed: turns, SubmittedQueues: submitted, FinalContext: pulled, FailureReason: st,
			}, nil
		}

		turns++
		qid := "Q-" + randomHex(6)
		submitted = append(submitted, qid)
		initialRec, err := c.SubmitQueueWithRecovery(ctx, MethodCallQueue{
			QueueID: qid, Epoch: synth.Epoch, BasedOnRev: synth.Rev,
			Trigger: "player_command", Supersedes: turns == 1, IntentSummary: dec.IntentSummary, Steps: filtered,
		})
		if err != nil {
			return nil, err
		}
		rec, err := c.WaitForQueueTerminal(ctx, qid, initialRec)
		if err != nil {
			return nil, err
		}
		lastRec = rec
		if initialRec == nil && rec != nil && rec.QueueState == "cancelled" {
			return &GoalExecutionReport{
				GoalID: goalID, Objective: objective, Succeeded: false, Status: "declined_by_player",
				TurnsUsed: turns, SubmittedQueues: submitted, FinalContext: lastCtx, FailureReason: "declined_by_player",
			}, nil
		}
		if rec != nil {
			for _, sr := range rec.StepReceipts {
				if sr.State == "failed" && sr.StepIndex >= 0 && sr.StepIndex < len(filtered) {
					st := filtered[sr.StepIndex]
					fp, _ := DigestSHA256("g2a.agent.loop.v1", map[string]any{
						"brief": synth.StateBrief, "method_id": st.MethodID, "actor": st.Actor, "args": st.Args,
					})
					failedFPs[fp] = struct{}{}
				}
			}
		}

		if fresh, _ := c.PullContextIfDue(ctx); fresh != nil {
			lastCtx = fresh
		}
		if successWhen != nil && lastCtx != nil && successWhen(*lastCtx, lastRec) {
			return &GoalExecutionReport{
				GoalID: goalID, Objective: objective, Succeeded: true, Status: "completed",
				TurnsUsed: turns, SubmittedQueues: submitted, FinalContext: lastCtx,
			}, nil
		}
		if successWhen == nil && lastRec != nil && lastRec.QueueState == "completed" && dec.GoalStatus != "continue" {
			return &GoalExecutionReport{
				GoalID: goalID, Objective: objective, Succeeded: true, Status: "completed",
				TurnsUsed: turns, SubmittedQueues: submitted, FinalContext: lastCtx,
			}, nil
		}
		time.Sleep(25 * time.Millisecond)
	}

	return &GoalExecutionReport{
		GoalID: goalID, Objective: objective, Succeeded: false, Status: "exhausted",
		TurnsUsed: turns, SubmittedQueues: submitted, FinalContext: lastCtx, FailureReason: "max_turns_exceeded",
	}, nil
}

func (c *CoreSDK) onContextReceived(ctx context.Context, pc ProjectedContext, depth int) (*QueueReceipt, error) {
	c.mu.Lock()
	if c.closed || c.actionsPaused || c.sessionID == "" {
		c.mu.Unlock()
		return nil, nil
	}
	c.metrics.ContextsReceived++
	cpCtx := pc
	c.latestContext = &cpCtx
	caps := c.capabilityCache[pc.CapabilityTableDigest]
	var playerID string
	if c.frozenScope != nil {
		playerID = c.frozenScope.PlayerID
	}
	c.mu.Unlock()

	var recalled []MemoryRecord
	if c.Memory != nil && playerID != "" {
		hint := ""
		if pc.PlayerCommand != nil {
			hint = *pc.PlayerCommand
		}
		recalled = c.Memory.RecallSafe(playerID, SpoilerGuard{
			CurrentEpoch:        pc.Epoch,
			AllowedTimelineTags: pc.AllowedTimelineTags,
			QueryHint:           hint,
			MaxResults:          48,
		})
	}

	decision, err := c.Planner.Plan(caps, pc, recalled)
	if err != nil {
		return nil, err
	}
	if decision.ReplyText != "" {
		_ = c.SendChat(ctx, decision.Audience, decision.ReplyText)
	}
	if len(decision.PlannedSteps) > 0 {
		isCmd := pc.PlayerCommand != nil && *pc.PlayerCommand != ""
		trigger := "proactive"
		if isCmd {
			trigger = "player_command"
		}
		q := MethodCallQueue{
			QueueID:       "Q-" + randomHex(6),
			Epoch:         pc.Epoch,
			BasedOnRev:    pc.Rev,
			Trigger:       trigger,
			Supersedes:    isCmd,
			IntentSummary: decision.IntentSummary,
			Steps:         decision.PlannedSteps,
		}
		return c.submitQueueDepth(ctx, q, depth)
	}
	return nil, nil
}

func (c *CoreSDK) handlePushFrame(ctx context.Context, raw []byte) {
	var env struct {
		EventID string          `json:"event_id"`
		Event   string          `json:"event"`
		Payload json.RawMessage `json:"payload"`
	}
	if err := UnmarshalStrict(raw, &env); err != nil {
		return
	}
	if env.EventID != "" {
		c.mu.Lock()
		if _, dup := c.seenSet[env.EventID]; dup {
			c.mu.Unlock()
			return
		}
		c.seenSet[env.EventID] = struct{}{}
		c.seenOrder = append(c.seenOrder, env.EventID)
		c.lastEventID = env.EventID
		if len(c.seenOrder) > maxSeenEventIDs {
			oldest := c.seenOrder[0]
			c.seenOrder = c.seenOrder[1:]
			delete(c.seenSet, oldest)
		}
		c.mu.Unlock()
	}

	switch env.Event {
	case "context.push":
		c.mu.Lock()
		c.nextAllowedPullMs = 0
		c.mu.Unlock()
		var pc ProjectedContext
		if err := UnmarshalStrict(env.Payload, &pc); err == nil {
			if c.OnContext != nil {
				c.OnContext(pc)
			}
			_, _ = c.onContextReceived(ctx, pc, 0)
		}
	case "capabilities.updated":
		var capEv struct {
			SlotID                   string              `json:"slot_id"`
			CapabilityTableDigest    string              `json:"capability_table_digest"`
			CapabilityTable          []CapabilityEntry   `json:"capability_table"`
			NarrowedPermittedMethods map[string][]string `json:"narrowed_permitted_methods"`
		}
		if err := UnmarshalStrict(env.Payload, &capEv); err == nil && len(capEv.CapabilityTable) > 0 {
			if verified, err := DigestSHA256("g2a.cap.v1", capEv.CapabilityTable); err == nil && verified == capEv.CapabilityTableDigest {
				c.mu.Lock()
				c.capabilityCache[capEv.CapabilityTableDigest] = capEv.CapabilityTable
				if capEv.SlotID != "" {
					c.slotDigests[capEv.SlotID] = capEv.CapabilityTableDigest
				}
				if c.frozenScope != nil && (capEv.SlotID == "" || capEv.SlotID == c.frozenScope.SlotID) {
					c.frozenScope.CapabilityTableDigest = capEv.CapabilityTableDigest
					if capEv.NarrowedPermittedMethods != nil {
						c.frozenScope.PermittedMethods = capEv.NarrowedPermittedMethods
					} else if c.frozenScope.PermittedMethods != nil {
						capActors := make(map[string]map[string]bool, len(capEv.CapabilityTable))
						for _, ce := range capEv.CapabilityTable {
							am := make(map[string]bool, len(ce.PermittedActors))
							for _, a := range ce.PermittedActors {
								am[a] = true
							}
							capActors[ce.MethodID] = am
						}
						narrowed := make(map[string][]string)
						for mid, prevActors := range c.frozenScope.PermittedMethods {
							allowedMap, exists := capActors[mid]
							if !exists {
								continue
							}
							var kept []string
							for _, a := range prevActors {
								if allowedMap[a] {
									kept = append(kept, a)
								}
							}
							if len(kept) > 0 {
								narrowed[mid] = kept
							}
						}
						c.frozenScope.PermittedMethods = narrowed
					}
				}
				c.mu.Unlock()
				if c.OnCapabilitiesUpdated != nil {
					c.OnCapabilitiesUpdated(capEv.CapabilityTable)
				}
			}
		}
	case "queue.confirmed":
		var p struct {
			QueueID string `json:"queue_id"`
		}
		if err := UnmarshalStrict(env.Payload, &p); err == nil && p.QueueID != "" {
			_, _ = c.OnEventQueueConfirmed(ctx, p.QueueID)
		}
	case "queue.declined":
		var p struct {
			QueueID    string `json:"queue_id"`
			CooldownMs int64  `json:"cooldown_ms"`
		}
		if err := UnmarshalStrict(env.Payload, &p); err == nil && p.QueueID != "" {
			c.OnEventQueueDeclined(p.QueueID, p.CooldownMs)
		}
	case "session.resumed":
		c.mu.Lock()
		c.actionsPaused = false
		c.mu.Unlock()
		var pc ProjectedContext
		if err := UnmarshalStrict(env.Payload, &pc); err == nil {
			_, _ = c.onContextReceived(ctx, pc, 0)
		}
	case "session.closed":
		c.Shutdown(context.Background(), false)
	}
}

func (c *CoreSDK) heartbeatLoop(ctx context.Context) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			sid := c.SessionID()
			if sid == "" {
				return
			}
			_, err := c.postEnvelope(ctx, "session.heartbeat", map[string]any{}, sid, nil)
			var gerr *G2AError
			if errors.As(err, &gerr) && (gerr.Code == "revoked" || gerr.Code == "permission_denied") {
				c.Shutdown(context.Background(), false)
				return
			}
		}
	}
}

func (c *CoreSDK) sseStreamLoop(ctx context.Context) {
	readyOnce := sync.Once{}
	isReconnect := false
	attempt := 0
	for ctx.Err() == nil {
		c.mu.Lock()
		base, tok, sid, lastID := c.hubEndpoint, c.authToken, c.sessionID, c.lastEventID
		c.mu.Unlock()
		if isReconnect {
			if _, err := c.ResumeSession(ctx); err != nil {
				var gerr *G2AError
				if errors.As(err, &gerr) && (gerr.Code == "revoked" || gerr.Code == "permission_denied") {
					c.Shutdown(context.Background(), false)
					return
				}
			}
		}
		u := fmt.Sprintf("%s/g2a/v1/core/events?session_id=%s", base, url.QueryEscape(sid))
		if isReconnect {
			u += "&replay=1"
			if lastID != "" {
				u += "&last_event_id=" + url.QueryEscape(lastID)
			}
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
		if err != nil {
			return
		}
		req.Header.Set("Accept", "text/event-stream")
		req.Header.Set("Authorization", "Bearer "+tok)
		if lastID != "" {
			req.Header.Set("Last-Event-ID", lastID)
		}
		sseClient := &http.Client{Transport: c.httpClient.Transport}
		resp, err := sseClient.Do(req)
		if err != nil {
			isReconnect = true
			sleepReconnectBackoff(ctx, &attempt)
			continue
		}
		attempt = 0
		readyOnce.Do(func() { close(c.streamReady) })
		scanner := bufio.NewScanner(resp.Body)
		scanner.Buffer(make([]byte, 64*1024), 2<<20)
		var dataLines []string
		for scanner.Scan() {
			line := scanner.Text()
			if line == "" {
				if len(dataLines) > 0 {
					joined := strings.Join(dataLines, "\n")
					dataLines = dataLines[:0]
					c.handlePushFrame(ctx, []byte(joined))
				}
			} else if strings.HasPrefix(line, "data:") {
				dataLines = append(dataLines, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
			}
		}
		_ = resp.Body.Close()
		isReconnect = true
		sleepReconnectBackoff(ctx, &attempt)
	}
}

func (c *CoreSDK) wsStreamLoop(ctx context.Context) {
	readyOnce := sync.Once{}
	isReconnect := false
	attempt := 0
	for ctx.Err() == nil {
		c.mu.Lock()
		base, tok, sid, lastID := c.hubEndpoint, c.authToken, c.sessionID, c.lastEventID
		c.mu.Unlock()
		if isReconnect {
			if _, err := c.ResumeSession(ctx); err != nil {
				var gerr *G2AError
				if errors.As(err, &gerr) && (gerr.Code == "revoked" || gerr.Code == "permission_denied") {
					c.Shutdown(context.Background(), false)
					return
				}
			}
		}
		conn, reader, err := dialRFC6455(ctx, base, sid, tok, isReconnect, lastID)
		if err != nil {
			isReconnect = true
			sleepReconnectBackoff(ctx, &attempt)
			continue
		}
		attempt = 0
		go func(cl net.Conn) {
			<-ctx.Done()
			_ = cl.Close()
		}(conn)
		readyOnce.Do(func() { close(c.streamReady) })
		for ctx.Err() == nil {
			opcode, payload, err := readWSServerFrame(reader)
			if err != nil {
				break
			}
			switch opcode {
			case 0x1: // Text
				c.handlePushFrame(ctx, payload)
			case 0x9: // Ping -> masked Pong
				_ = writeWSMaskedControlFrame(conn, 0xA, payload)
			case 0x8: // Close
				_ = writeWSMaskedControlFrame(conn, 0x8, nil)
				_ = conn.Close()
				goto reconnect
			}
		}
	reconnect:
		_ = conn.Close()
		isReconnect = true
		sleepReconnectBackoff(ctx, &attempt)
	}
}

func sleepReconnectBackoff(ctx context.Context, attempt *int) {
	shift := *attempt
	if shift > 5 {
		shift = 5
	}
	*attempt = *attempt + 1
	ms := 150 * (1 << shift)
	if ms > 4000 {
		ms = 4000
	}
	t := time.NewTimer(time.Duration(ms) * time.Millisecond)
	defer t.Stop()
	select {
	case <-ctx.Done():
	case <-t.C:
	}
}

func dialRFC6455(ctx context.Context, base, sid, tok string, isReconnect bool, lastID string) (net.Conn, *bufio.Reader, error) {
	u, err := url.Parse(base)
	if err != nil {
		return nil, nil, err
	}
	hostPort := u.Host
	if u.Port() == "" {
		if u.Scheme == "https" {
			hostPort = net.JoinHostPort(u.Hostname(), "443")
		} else {
			hostPort = net.JoinHostPort(u.Hostname(), "80")
		}
	}
	var d net.Dialer
	rawConn, err := d.DialContext(ctx, "tcp", hostPort)
	if err != nil {
		return nil, nil, err
	}
	conn := rawConn
	if u.Scheme == "https" {
		tlsConn := tls.Client(rawConn, &tls.Config{ServerName: u.Hostname(), MinVersion: tls.VersionTLS13})
		if err := tlsConn.HandshakeContext(ctx); err != nil {
			_ = rawConn.Close()
			return nil, nil, err
		}
		conn = tlsConn
	}

	var keyBytes [16]byte
	_, _ = rand.Read(keyBytes[:])
	wsKey := base64.StdEncoding.EncodeToString(keyBytes[:])
	sum := sha1.Sum([]byte(wsKey + wsMagicGUID))
	expectedAccept := base64.StdEncoding.EncodeToString(sum[:])

	path := strings.TrimRight(u.Path, "/") + "/g2a/v1/core/ws?session_id=" + url.QueryEscape(sid)
	if isReconnect {
		path += "&replay=1"
		if lastID != "" {
			path += "&last_event_id=" + url.QueryEscape(lastID)
		}
	}
	var reqBuf bytes.Buffer
	fmt.Fprintf(&reqBuf, "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer %s\r\n",
		path, u.Host, wsKey, tok)
	if lastID != "" {
		fmt.Fprintf(&reqBuf, "Last-Event-ID: %s\r\n", lastID)
	}
	reqBuf.WriteString("\r\n")
	if _, err := conn.Write(reqBuf.Bytes()); err != nil {
		_ = conn.Close()
		return nil, nil, err
	}

	reader := bufio.NewReader(conn)
	statusLine, err := reader.ReadString('\n')
	if err != nil || !strings.Contains(statusLine, " 101 ") {
		_ = conn.Close()
		return nil, nil, errors.New("WebSocket upgrade failed")
	}
	gotAccept := ""
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			_ = conn.Close()
			return nil, nil, err
		}
		trimmed := strings.TrimRight(line, "\r\n")
		if trimmed == "" {
			break
		}
		if k, v, ok := strings.Cut(trimmed, ":"); ok && strings.EqualFold(strings.TrimSpace(k), "Sec-WebSocket-Accept") {
			gotAccept = strings.TrimSpace(v)
		}
	}
	if gotAccept != expectedAccept {
		_ = conn.Close()
		return nil, nil, errors.New("RFC 6455 Sec-WebSocket-Accept verification failed")
	}
	return conn, reader, nil
}

func readWSServerFrame(r *bufio.Reader) (byte, []byte, error) {
	var hdr [2]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return 0, nil, err
	}
	opcode := hdr[0] & 0x0F
	masked := (hdr[1] & 0x80) != 0
	length := uint64(hdr[1] & 0x7F)
	if length == 126 {
		var ext [2]byte
		if _, err := io.ReadFull(r, ext[:]); err != nil {
			return 0, nil, err
		}
		length = uint64(binary.BigEndian.Uint16(ext[:]))
	} else if length == 127 {
		var ext [8]byte
		if _, err := io.ReadFull(r, ext[:]); err != nil {
			return 0, nil, err
		}
		length = binary.BigEndian.Uint64(ext[:])
	}
	if length > (2 << 20) {
		return 0, nil, errors.New("WS frame exceeds 2 MiB")
	}
	var maskKey [4]byte
	if masked {
		if _, err := io.ReadFull(r, maskKey[:]); err != nil {
			return 0, nil, err
		}
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(r, payload); err != nil {
		return 0, nil, err
	}
	if masked {
		for i := range payload {
			payload[i] ^= maskKey[i%4]
		}
	}
	return opcode, payload, nil
}

func writeWSMaskedControlFrame(conn net.Conn, opcode byte, payload []byte) error {
	if len(payload) > 125 {
		payload = payload[:125]
	}
	var mask [4]byte
	_, _ = rand.Read(mask[:])
	frame := make([]byte, 0, 6+len(payload))
	frame = append(frame, 0x80|opcode, 0x80|byte(len(payload)))
	frame = append(frame, mask[:]...)
	for i, b := range payload {
		frame = append(frame, b^mask[i%4])
	}
	_, err := conn.Write(frame)
	return err
}

func (c *CoreSDK) postEnvelope(
	ctx context.Context,
	operation string,
	payload any,
	sessionID string,
	extensions map[string]any,
) (json.RawMessage, error) {
	if c.CustomPostTransport != nil {
		return c.CustomPostTransport(ctx, operation, payload, sessionID)
	}
	c.mu.Lock()
	base, tok := c.hubEndpoint, c.authToken
	c.mu.Unlock()

	if extensions == nil {
		extensions = map[string]any{}
	}
	env := map[string]any{
		"g2a_version":  G2AWireVersion,
		"request_id":   "go-core-" + randomHex(6),
		"timestamp_ms": time.Now().UnixMilli(),
		"operation":    operation,
		"payload":      payload,
		"extensions":   extensions,
	}
	if sessionID != "" {
		env["session_id"] = sessionID
	}
	rawReq, err := json.Marshal(env)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/g2a/v1/core", bytes.NewReader(rawReq))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		if ctx.Err() == nil {
			var netErr net.Error
			if (errors.As(err, &netErr) && netErr.Timeout()) || errors.Is(err, context.DeadlineExceeded) {
				return nil, &G2AError{Code: "outcome_unknown", HTTPStatus: 504, Message: "HTTP request timed out: " + err.Error()}
			}
		}
		return nil, err
	}
	defer resp.Body.Close()
	respBytes, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	var reply struct {
		Acceptance string          `json:"acceptance"`
		Result     json.RawMessage `json:"result"`
		Error      *G2AError       `json:"error"`
	}
	if err := UnmarshalStrict(respBytes, &reply); err != nil {
		return nil, err
	}
	if reply.Acceptance == "accepted" {
		return reply.Result, nil
	}
	gerr := reply.Error
	if gerr == nil {
		gerr = &G2AError{Code: "invalid_request", HTTPStatus: resp.StatusCode, Message: "Request not accepted"}
	}
	if gerr.RetryAfterMs == nil {
		if ra := resp.Header.Get("Retry-After"); ra != "" {
			if secs, pErr := strconv.ParseInt(strings.TrimSpace(ra), 10, 64); pErr == nil {
				ms := secs * 1000
				gerr.RetryAfterMs = &ms
			}
		}
	}
	return nil, gerr
}

func randomHex(nBytes int) string {
	b := make([]byte, nBytes)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
