package hub_test

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/hub"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/jcs"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/model"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/store"
)

type fakeOrigin struct {
	mu      sync.Mutex
	calls   []model.OriginExecuteRequest
	handler func(req model.OriginExecuteRequest) model.OriginExecuteReply
}

func (f *fakeOrigin) Execute(_ string, req model.OriginExecuteRequest) (model.OriginExecuteReply, *model.G2AError) {
	f.mu.Lock()
	f.calls = append(f.calls, req)
	h := f.handler
	f.mu.Unlock()
	if h != nil {
		return h(req), nil
	}
	return model.OriginExecuteReply{ExecutionID: req.ExecutionID, State: "succeeded"}, nil
}

func TestStrictJSONAndJCSCanonicalDigest(t *testing.T) {
	if _, err := jcs.ParseStrictJSON([]byte(`{"a":1,"a":2}`)); err == nil {
		t.Fatal("expected duplicate key error")
	}
	if _, err := jcs.DigestSHA256("g2a.test.v1", map[string]any{"x": math.NaN()}); err == nil {
		t.Fatal("expected NaN rejection")
	}
	d1, err := jcs.DigestSHA256("g2a.confirm.v1", map[string]any{
		"steps":      []any{map[string]any{"args": map[string]any{"qty": 20.0, "item": "timber"}, "step_index": 0}},
		"queue_id":   "Q-205",
		"session_id": "sess-1",
	})
	if err != nil {
		t.Fatalf("unexpected jcs error: %v", err)
	}
	d2, err := jcs.DigestSHA256("g2a.confirm.v1", map[string]any{
		"session_id": "sess-1",
		"queue_id":   "Q-205",
		"steps":      []any{map[string]any{"step_index": 0, "args": map[string]any{"item": "timber", "qty": 20}}},
	})
	if err != nil {
		t.Fatalf("unexpected jcs error: %v", err)
	}
	if d1 != d2 {
		t.Fatalf("canonical digests differed: %s vs %s", d1, d2)
	}
	const golden = "sha256:8d8634fd72ae48e655a9151e50b06a3e1095e753cacb2c293706e59af1b9ccd8"
	if d1 != golden {
		t.Fatalf("expected golden cross-language digest %s, got %s", golden, d1)
	}
}

func TestWalkthrough1SustainedStepAndAtomicSettledSync(t *testing.T) {
	ledgerFile := filepath.Join(t.TempDir(), "hub_ledger.json")
	p, err := store.NewPersistencePort(ledgerFile)
	if err != nil {
		t.Fatal(err)
	}
	cfg := hub.DefaultConfig()
	cfg.AutoApproveScope = true

	var eng *hub.Engine
	fo := &fakeOrigin{}
	eng = hub.NewEngine(cfg, p, fo, nil, nil)

	caps := []model.CapabilityEntry{
		{
			MethodID: "#01", Name: "move_to", Description: "Move to target",
			PermittedActors: []string{"companion"},
			ParamsSchema:    map[string]any{"type": "object", "required": []any{"target_id"}},
			Sustained:       true, DefaultPolicy: "allow",
		},
		{
			MethodID: "#02", Name: "interact", Description: "Interact with target",
			PermittedActors: []string{"companion"},
			ParamsSchema:    map[string]any{"type": "object", "required": []any{"target_id"}},
			Sustained:       false, DefaultPolicy: "allow",
		},
	}
	capDigest, _ := jcs.DigestSHA256("g2a.cap.v1", caps)
	if gErr := eng.HandleOriginCapabilities(model.OriginCapabilitiesPayload{
		SlotID: "slot-comp", RoleKind: "companion", BillingMode: "player_metered",
		CapabilityTableDigest: capDigest, Capabilities: caps, OriginExecuteURL: "mem://origin",
	}); gErr != nil {
		t.Fatalf("capabilities err: %v", gErr)
	}

	// Sync rev=18: #01 ready, #02 staged for chest_R (while nearby chest_L is ready, testing target-level readiness!)
	if gErr := eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-comp", Epoch: 1, Rev: 18, Mode: "full", SignificantEvent: true,
		StateBrief: map[string]any{"hp": 100},
		UpsertedMethods: []model.ActiveMethodGrant{
			{
				MethodID: "#01", Actor: "companion", Readiness: "ready",
				Targets: []model.TargetCandidate{{TargetID: "chest_R", Label: "Right Chest", Readiness: "ready"}},
			},
			{
				MethodID: "#02", Actor: "companion", Readiness: "ready",
				Targets: []model.TargetCandidate{
					{TargetID: "chest_L", Label: "Left Chest", Readiness: "ready"},
					{TargetID: "chest_R", Label: "Right Chest", Readiness: "staged"},
				},
			},
		},
	}); gErr != nil {
		t.Fatalf("sync err: %v", gErr)
	}

	authRep, gErr := eng.HandleSessionAuthorize("0.0.1", "core-alpha", model.AuthorizeRequest{
		SlotID:         "slot-comp",
		RequestedScope: model.RequestedScopeSpec{AllowChat: true},
	})
	if gErr != nil {
		t.Fatalf("authorize err: %v", gErr)
	}

	// Target-level readiness guard: trying to open chest_R directly as step 0 must fail with stale_context!
	_, badErr := eng.HandleQueueSubmit("core-alpha", authRep.SessionID, model.MethodCallQueue{
		QueueID: "Q-bad", Epoch: 1, BasedOnRev: 18, Trigger: "player_command",
		Steps: []model.QueueStep{{StepIndex: 0, MethodID: "#02", Actor: "companion", Args: map[string]any{"target_id": "chest_R"}}},
	})
	if badErr == nil || badErr.Code != model.ErrStaleContext {
		t.Fatalf("expected stale_context for staged target_id chest_R, got %v", badErr)
	}

	// Configure fakeOrigin: #01 returns executing and triggers atomic rev=19 delta frame settling execID and turning chest_R ready!
	fo.handler = func(req model.OriginExecuteRequest) model.OriginExecuteReply {
		if req.MethodID != nil && *req.MethodID == "#01" {
			go func(execID string) {
				time.Sleep(20 * time.Millisecond)
				base := int64(18)
				_ = eng.HandleOriginSync(model.OriginSyncFrame{
					SlotID: "slot-comp", Epoch: 1, Rev: 19, BaseRev: &base, Mode: "delta",
					StateBrief: map[string]any{"hp": 100},
					UpsertedMethods: []model.ActiveMethodGrant{
						{
							MethodID: "#02", Actor: "companion", Readiness: "ready",
							Targets: []model.TargetCandidate{{TargetID: "chest_R", Label: "Right Chest", Readiness: "ready"}},
						},
					},
					SettledExecutions: []model.OriginExecuteReply{
						{ExecutionID: execID, State: "succeeded", EffectSummary: map[string]any{"arrived": "chest_R"}},
					},
				})
			}(req.ExecutionID)
			return model.OriginExecuteReply{ExecutionID: req.ExecutionID, State: "executing"}
		}
		return model.OriginExecuteReply{
			ExecutionID:   req.ExecutionID,
			State:         "succeeded",
			EffectSummary: map[string]any{"loot": []string{"gold_50", "potion_1"}},
		}
	}

	receipt, gErr := eng.HandleQueueSubmit("core-alpha", authRep.SessionID, model.MethodCallQueue{
		QueueID: "Q-101", Epoch: 1, BasedOnRev: 18, Trigger: "player_command", Supersedes: true,
		IntentSummary: "Move to right chest and open it",
		Steps: []model.QueueStep{
			{StepIndex: 0, MethodID: "#01", Actor: "companion", Args: map[string]any{"target_id": "chest_R"}},
			{StepIndex: 1, MethodID: "#02", Actor: "companion", Args: map[string]any{"target_id": "chest_R"}},
		},
	})
	if gErr != nil {
		t.Fatalf("Q-101 submit failed: %v", gErr)
	}
	if receipt.QueueState != "completed" || len(receipt.StepReceipts) != 2 || receipt.StepReceipts[1].State != "succeeded" {
		t.Fatalf("unexpected receipt: %+v", receipt)
	}
}

func TestWalkthrough2ProactiveConfirmDigestAndOfflineStop(t *testing.T) {
	p, _ := store.NewPersistencePort("")
	cfg := hub.DefaultConfig()
	cfg.AutoApproveScope = true
	fo := &fakeOrigin{}
	eng := hub.NewEngine(cfg, p, fo, nil, nil)

	caps := []model.CapabilityEntry{
		{
			MethodID: "#05", Name: "trade_resource", Description: "Buy resource",
			PermittedActors: []string{"player"},
			ParamsSchema:    map[string]any{"type": "object", "required": []any{"item", "qty"}},
			Sustained:       false, DefaultPolicy: "allow",
		},
	}
	capDigest, _ := jcs.DigestSHA256("g2a.cap.v1", caps)
	_ = eng.HandleOriginCapabilities(model.OriginCapabilitiesPayload{
		SlotID: "slot-sim", RoleKind: "companion", BillingMode: "player_metered",
		CapabilityTableDigest: capDigest, Capabilities: caps, OriginExecuteURL: "mem://sim",
	})
	_ = eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-sim", Epoch: 1, Rev: 42, Mode: "full", SignificantEvent: true,
		StateBrief: map[string]any{"gold": 500},
		UpsertedMethods: []model.ActiveMethodGrant{
			{MethodID: "#05", Actor: "player", Readiness: "ready", Targets: []model.TargetCandidate{}},
		},
	})
	authRep, _ := eng.HandleSessionAuthorize("0.0.1", "core-sim", model.AuthorizeRequest{SlotID: "slot-sim"})

	q := model.MethodCallQueue{
		QueueID: "Q-205", Epoch: 1, BasedOnRev: 42, Trigger: "proactive",
		IntentSummary: "Buy 20 timber",
		Steps: []model.QueueStep{
			{StepIndex: 0, MethodID: "#05", Actor: "player", Args: map[string]any{"item": "timber", "qty": 20}},
		},
	}

	// Proactive + actor=="player" MUST escalate to 409 confirmation_required and commit awaiting_confirmation!
	_, holdErr := eng.HandleQueueSubmit("core-sim", authRep.SessionID, q)
	if holdErr == nil || holdErr.Code != model.ErrConfirmationRequired {
		t.Fatalf("expected confirmation_required, got %v", holdErr)
	}

	// Tampering arguments under the same queue_id MUST return 409 conflict!
	tampered := q
	tampered.Steps = []model.QueueStep{
		{StepIndex: 0, MethodID: "#05", Actor: "player", Args: map[string]any{"item": "timber", "qty": 999}},
	}
	_, confErr := eng.HandleQueueSubmit("core-sim", authRep.SessionID, tampered)
	if confErr == nil || confErr.Code != model.ErrConflict {
		t.Fatalf("expected 409 conflict on tampered queue_id reuse, got %v", confErr)
	}

	// Approve via Trusted Consent and resubmit identical Q-205 -> must complete!
	eng.OnTrustedConfirmationResult(authRep.SessionID, "Q-205", "", "", true)
	rec, gErr := eng.HandleQueueSubmit("core-sim", authRep.SessionID, q)
	if gErr != nil || rec.QueueState != "completed" {
		t.Fatalf("expected completed after approval, got rec=%+v err=%v", rec, gErr)
	}

	// Mandatory offline stop: player goes offline -> session closes and heartbeat returns 403 revoked!
	_ = eng.HandleOriginPlayerEvent(model.OriginPlayerEvent{
		SlotID: "slot-sim", Kind: "presence_changed", Online: false, RemainingOnlineHumans: 0,
	})
	if _, hbErr := eng.HandleSessionHeartbeat("core-sim", authRep.SessionID); hbErr == nil || hbErr.Code != model.ErrRevoked {
		t.Fatalf("expected 403 revoked after player offline, got %v", hbErr)
	}
}

func TestPolicyOverrideRealMoneyGuardAndLeaseReaperResume(t *testing.T) {
	nowMs := int64(100000)
	p, _ := store.NewPersistencePort("")
	cfg := hub.DefaultConfig()
	cfg.AutoApproveScope = true
	cfg.LeaseTTLMs = 5000
	cfg.ClockNowMs = func() int64 { return nowMs }
	eng := hub.NewEngine(cfg, p, &fakeOrigin{}, nil, nil)

	caps := []model.CapabilityEntry{
		{
			MethodID: "#04", Name: "toggle_flashlight", Description: "Flashlight",
			PermittedActors: []string{"companion"},
			ParamsSchema:    map[string]any{"type": "object"},
			DefaultPolicy:   "allow",
		},
		{
			MethodID: "#09", Name: "buy_dlc_pack", Description: "Real money DLC",
			PermittedActors: []string{"player"},
			ParamsSchema:    map[string]any{"type": "object"},
			RiskFlags:       []string{"real_money"}, DefaultPolicy: "confirm",
		},
	}
	capDigest, _ := jcs.DigestSHA256("g2a.cap.v1", caps)
	_ = eng.HandleOriginCapabilities(model.OriginCapabilitiesPayload{
		SlotID: "slot-gov", RoleKind: "companion", BillingMode: "player_metered",
		CapabilityTableDigest: capDigest, Capabilities: caps, OriginExecuteURL: "mem://gov",
	})
	_ = eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-gov", Epoch: 1, Rev: 1, Mode: "full", SignificantEvent: true,
		StateBrief: map[string]any{},
		UpsertedMethods: []model.ActiveMethodGrant{
			{MethodID: "#04", Actor: "companion", Readiness: "ready", Targets: []model.TargetCandidate{}},
			{MethodID: "#09", Actor: "player", Readiness: "ready", Targets: []model.TargetCandidate{}},
		},
	})

	// 1. Downgrading a real_money capability to auto-allow MUST be rejected!
	if err := eng.SetPolicyOverride("slot-gov", "#09", "player", "allow"); err == nil || err.Code != model.ErrPermissionDenied {
		t.Fatalf("expected permission_denied when downgrading real_money to allow, got %v", err)
	}

	// 2. Player overrides #04 to prohibit -> queue submission rejected!
	if err := eng.SetPolicyOverride("slot-gov", "#04", "companion", "prohibit"); err != nil {
		t.Fatalf("unexpected override err: %v", err)
	}
	authRep, _ := eng.HandleSessionAuthorize("0.0.1", "core-gov", model.AuthorizeRequest{SlotID: "slot-gov"})
	_, pErr := eng.HandleQueueSubmit("core-gov", authRep.SessionID, model.MethodCallQueue{
		QueueID: "Q-prohibit", Epoch: 1, BasedOnRev: 1, Trigger: "player_command",
		Steps: []model.QueueStep{{StepIndex: 0, MethodID: "#04", Actor: "companion", Args: map[string]any{}}},
	})
	if pErr == nil || pErr.Code != model.ErrPermissionDenied {
		t.Fatalf("expected permission_denied on prohibited override, got %v", pErr)
	}

	// 3. Lease expiration reaper -> Tier-2 session.resume returns ActionsPaused == true until player approves!
	nowMs += 6000
	if reaped := eng.ReapExpiredSessionLeasesOnce(); reaped != 1 {
		t.Fatalf("expected 1 reaped session, got %d", reaped)
	}
	resRep, rErr := eng.HandleSessionResume("core-gov", authRep.SessionID)
	if rErr != nil || !resRep.ActionsPaused {
		t.Fatalf("expected ActionsPaused=true after lease expiry, got rep=%+v err=%v", resRep, rErr)
	}
	eng.OnTrustedResumeDecision(authRep.SessionID, true)
	st := eng.SnapshotDashboardStatus()
	sessions := st["sessions"].([]map[string]any)
	if sessions[0]["state"] != "active" {
		t.Fatalf("expected session active after trusted resume approval, got %v", sessions[0]["state"])
	}
}

func TestCrashRecoverySanitizerAndBoundedPruner(t *testing.T) {
	ledgerPath := filepath.Join(t.TempDir(), "crashed_hub.json")

	// Simulate an uncleanly terminated Hub ledger with an "active" session and a mid-flight "running" queue
	crashed := store.NewHubStoreData()
	crashed.Slots["slot-crash"] = &store.SlotRecord{
		SlotID: "slot-crash", RoleKind: "companion", BillingMode: "player_metered",
		PlayerID: "p1", GameInstanceID: "g1", PlayerOnline: true,
		CapabilityTableDigest: "sha256:dummy",
	}
	qID := "Q-in-flight"
	execID := "exec-99"
	crashed.Sessions["sess-crash"] = &store.SessionRecord{
		SessionID:       "sess-crash",
		CallerCoreID:    "core-1",
		Scope:           model.FrozenScope{SlotID: "slot-crash", RoleKind: "companion", BillingMode: "player_metered", AllowChat: true},
		State:           "active",
		PlayerOnline:    true,
		LeaseDeadlineMs: time.Now().UnixMilli() + 60000,
		ActiveQueueID:   &qID,
	}
	crashed.Queues[store.QueueKey("sess-crash", qID)] = &store.QueueLedgerRecord{
		SessionID:          "sess-crash",
		QueueID:            qID,
		State:              "running",
		CurrentExecutionID: &execID,
		StepReceipts: []model.StepReceipt{
			{StepIndex: 0, MethodID: "#01", Actor: "companion", State: "executing"},
			{StepIndex: 1, MethodID: "#02", Actor: "companion", State: "pending"},
		},
	}
	raw, _ := json.Marshal(crashed)
	if err := os.WriteFile(ledgerPath, raw, 0o600); err != nil {
		t.Fatalf("write crashed ledger: %v", err)
	}

	p, err := store.NewPersistencePort(ledgerPath)
	if err != nil {
		t.Fatalf("reload persistence: %v", err)
	}
	defer func() { _ = p.Close() }()

	eng := hub.NewEngine(hub.DefaultConfig(), p, &fakeOrigin{}, nil, nil)
	resRep, gErr := eng.HandleSessionResume("core-1", "sess-crash")
	if gErr != nil || !resRep.ActionsPaused {
		t.Fatalf("expected crashed active session to require paused resume, got rep=%+v err=%v", resRep, gErr)
	}
	qRep, qErr := eng.HandleQueueQuery("core-1", "sess-crash", qID)
	if qErr != nil || qRep.QueueState != "aborted" {
		t.Fatalf("expected interrupted running queue to be sanitized to aborted, got rep=%+v err=%v", qRep, qErr)
	}
	if qRep.StepReceipts[0].FailureReason == nil || *qRep.StepReceipts[0].FailureReason != "hub_restarted_mid_execution" {
		t.Fatalf("unexpected step failure reason after crash sanitizer: %+v", qRep.StepReceipts[0])
	}

	// Verify bounded ChatDedup and terminal Queue pruning
	_, _ = store.MutateVolatile(p, func(d *store.HubStoreData) (struct{}, *model.G2AError) {
		for i := 0; i < 2400; i++ {
			d.RecordChatDedup(fmt.Sprintf("sess-crash|msg-%d", i))
		}
		for i := 0; i < 600; i++ {
			qk := store.QueueKey("sess-crash", fmt.Sprintf("Q-done-%d", i))
			d.RegisterQueue(qk, &store.QueueLedgerRecord{
				SessionID: "sess-crash",
				QueueID:   fmt.Sprintf("Q-done-%d", i),
				State:     "completed",
			})
		}
		if len(d.ChatDedup) > store.MaxChatDedupEntries {
			t.Fatalf("ChatDedup exceeded bound: %d > %d", len(d.ChatDedup), store.MaxChatDedupEntries)
		}
		if len(d.Queues) > store.MaxTerminalQueuesStored {
			t.Fatalf("Queues exceeded bound: %d > %d", len(d.Queues), store.MaxTerminalQueuesStored)
		}
		return struct{}{}, nil
	})
}

func TestConcurrentHighFrequencyDeltaSyncAndReadOnlyQueries(t *testing.T) {
	ledgerPath := filepath.Join(t.TempDir(), "fast_hub.json")
	p, err := store.NewPersistencePort(ledgerPath)
	if err != nil {
		t.Fatalf("new persistence: %v", err)
	}
	defer func() { _ = p.Close() }()

	cfg := hub.DefaultConfig()
	cfg.AutoApproveScope = true
	eng := hub.NewEngine(cfg, p, &fakeOrigin{}, nil, nil)

	caps := []model.CapabilityEntry{
		{
			MethodID: "#01", Name: "ping", Description: "Ping",
			PermittedActors: []string{"companion"}, ParamsSchema: map[string]any{"type": "object"},
			DefaultPolicy: "allow",
		},
	}
	capDigest, _ := jcs.DigestSHA256("g2a.cap.v1", caps)
	_ = eng.HandleOriginCapabilities(model.OriginCapabilitiesPayload{
		SlotID: "slot-perf", RoleKind: "companion", BillingMode: "player_metered",
		CapabilityTableDigest: capDigest, Capabilities: caps, OriginExecuteURL: "mem://perf",
	})
	_ = eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-perf", Epoch: 1, Rev: 1, Mode: "full", SignificantEvent: true,
		StateBrief: map[string]any{"tick": 0},
		UpsertedMethods: []model.ActiveMethodGrant{
			{MethodID: "#01", Actor: "companion", Readiness: "ready", Targets: []model.TargetCandidate{}},
		},
	})
	authRep, aErr := eng.HandleSessionAuthorize("0.0.1", "core-perf", model.AuthorizeRequest{SlotID: "slot-perf"})
	if aErr != nil {
		t.Fatalf("authorize err: %v", aErr)
	}

	start := time.Now()
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for rev := int64(2); rev <= 250; rev++ {
			prev := rev - 1
			if sErr := eng.HandleOriginSync(model.OriginSyncFrame{
				SlotID: "slot-perf", Epoch: 1, Rev: rev, BaseRev: &prev, Mode: "delta",
				SignificantEvent: rev%50 == 0,
				StateBrief:       map[string]any{"tick": rev},
			}); sErr != nil {
				t.Errorf("delta sync %d failed: %v", rev, sErr)
				return
			}
		}
	}()

	for r := 0; r < 6; r++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 80; i++ {
				_ = eng.SnapshotDashboardStatus()
				_, _ = eng.HandleSessionHeartbeat("core-perf", authRep.SessionID)
			}
		}()
	}
	wg.Wait()
	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Fatalf("high-frequency delta sync + concurrent readers took too long: %v", elapsed)
	}
}

func TestOutboundOriginNewestSubscriberPriorityAndEventStreamLRU(t *testing.T) {
	// 1. Verify OriginOutboundStreamBroker routes to the newest connected subscriber first (avoiding half-open stale socket shadowing)
	broker := hub.NewOriginOutboundStreamBroker(2 * time.Second)
	oldCh, unsubOld := broker.SubscribeSlot("slot-reconn")
	defer unsubOld()
	newCh, unsubNew := broker.SubscribeSlot("slot-reconn")
	defer unsubNew()

	doneCh := make(chan model.OriginExecuteReply, 1)
	errCh := make(chan *model.G2AError, 1)
	go func() {
		mID := "#02"
		act := "companion"
		rep, gErr := broker.DispatchOverStream(model.OriginExecuteRequest{
			SlotID:      "slot-reconn",
			Epoch:       1,
			Kind:        "invoke",
			ExecutionID: "exec-reconn-1",
			MethodID:    &mID,
			Actor:       &act,
			Args:        map[string]any{"target_id": "chest_1"},
		})
		if gErr != nil {
			errCh <- gErr
			return
		}
		doneCh <- rep
	}()

	select {
	case env := <-newCh:
		select {
		case <-oldCh:
			t.Fatal("stale oldCh should not have received the request when newCh is healthy")
		default:
		}
		rawRes, _ := json.Marshal(model.OriginExecuteReply{
			ExecutionID:   "exec-reconn-1",
			State:         "succeeded",
			EffectSummary: map[string]any{"opened": true},
		})
		if !broker.DeliverReply(hub.OutboundReplyEnvelope{
			G2AVersion: "0.0.1",
			RequestID:  env.RequestID,
			Acceptance: model.AcceptanceAccepted,
			Result:     rawRes,
		}) {
			t.Fatal("expected DeliverReply to match pending waiter")
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for newCh to receive origin.execute")
	}

	select {
	case gErr := <-errCh:
		t.Fatalf("unexpected DispatchOverStream error: %v", gErr)
	case rep := <-doneCh:
		if rep.State != "succeeded" || rep.EffectSummary["opened"] != true {
			t.Fatalf("unexpected execute reply: %+v", rep)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for DispatchOverStream completion")
	}

	// 2. Verify EventStreamHub rotates active subscriber sessions in historyOrder instead of orphaning them
	esHub := hub.NewEventStreamHub()
	liveCh, unsubLive := esHub.Subscribe("sess-live-0", false)
	esHub.PushEvent("sess-live-0", "context.push", map[string]any{"rev": 1})
	<-liveCh

	for i := 1; i <= 310; i++ {
		esHub.PushEvent(fmt.Sprintf("sess-idle-%d", i), "context.push", map[string]any{"rev": i})
	}
	// While sess-live-0 still has an active subscriber, replay=true on a second subscriber MUST still find its history!
	replayCh, unsubReplay := esHub.Subscribe("sess-live-0", true)
	select {
	case ev := <-replayCh:
		if ev.SessionID != "sess-live-0" {
			t.Fatalf("unexpected replayed session: %s", ev.SessionID)
		}
	default:
		t.Fatal("expected active session history to survive LRU sweeps")
	}
	unsubReplay()
	unsubLive()

	// Once sess-live-0 has zero subscribers, subsequent 300 idle sessions must cleanly evict its history too!
	for i := 400; i <= 700; i++ {
		esHub.PushEvent(fmt.Sprintf("sess-idle-%d", i), "context.push", map[string]any{"rev": i})
	}
	afterEvictCh, unsubAfter := esHub.Subscribe("sess-live-0", true)
	defer unsubAfter()
	select {
	case <-afterEvictCh:
		t.Fatal("expected unsubscribed old session history to be evicted instead of leaked")
	default:
	}
}

func TestConfirmationTimeoutQuietBackoffSustainedCancelCapRefreshAndNpcHandoff(t *testing.T) {
	var mu sync.Mutex
	nowMs := int64(200000)
	advanceClock := func(delta int64) {
		mu.Lock()
		nowMs += delta
		mu.Unlock()
	}

	p, err := store.NewPersistencePort("")
	if err != nil {
		t.Fatal(err)
	}
	cfg := hub.DefaultConfig()
	cfg.AutoApproveScope = true
	cfg.MinSyncIntervalMs = 100
	cfg.MaxIdleSleepIntervalMs = 800
	cfg.ConfirmPromptTimeoutMs = 500
	cfg.DefaultConfirmCooldownMs = 5000
	cfg.SustainedStepTimeoutMs = 60
	cfg.ClockNowMs = func() int64 {
		mu.Lock()
		defer mu.Unlock()
		return nowMs
	}

	fo := &fakeOrigin{}
	eng := hub.NewEngine(cfg, p, fo, nil, nil)

	caps := []model.CapabilityEntry{
		{
			MethodID: "#01", Name: "warp_charge", Description: "Sustained warp charge",
			PermittedActors: []string{"companion"},
			ParamsSchema:    map[string]any{"type": "object"},
			Sustained:       true, DefaultPolicy: "allow",
		},
		{
			MethodID: "#04", Name: "flashlight", Description: "Toggle flashlight",
			PermittedActors: []string{"companion"},
			ParamsSchema:    map[string]any{"type": "object"},
			DefaultPolicy:   "allow",
		},
		{
			MethodID: "#05", Name: "buy_supply", Description: "Spend player gold",
			PermittedActors: []string{"player"},
			ParamsSchema:    map[string]any{"type": "object", "required": []any{"qty"}},
			DefaultPolicy:   "allow",
		},
	}
	capDigest, _ := jcs.DigestSHA256("g2a.cap.v1", caps)
	if gErr := eng.HandleOriginCapabilities(model.OriginCapabilitiesPayload{
		SlotID: "slot-wave14", RoleKind: "companion", BillingMode: "player_metered",
		PlayerID: "player-A", CapabilityTableDigest: capDigest, Capabilities: caps, OriginExecuteURL: "mem://wave14",
	}); gErr != nil {
		t.Fatalf("capabilities: %v", gErr)
	}
	_ = eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-wave14", Epoch: 1, Rev: 1, Mode: "full", SignificantEvent: true,
		StateBrief: map[string]any{"tick": 1},
		UpsertedMethods: []model.ActiveMethodGrant{
			{MethodID: "#01", Actor: "companion", Readiness: "ready"},
			{MethodID: "#04", Actor: "companion", Readiness: "ready"},
			{MethodID: "#05", Actor: "player", Readiness: "ready"},
		},
	})
	authRep, gErr := eng.HandleSessionAuthorize("0.0.1", "core-wave14", model.AuthorizeRequest{
		SlotID:         "slot-wave14",
		PlayerID:       "player-A",
		RequestedScope: model.RequestedScopeSpec{AllowChat: true},
	})
	if gErr != nil {
		t.Fatalf("authorize: %v", gErr)
	}
	evCh, unsub := eng.PushStream().Subscribe(authRep.SessionID, false)
	defer unsub()

	// 1. Strict ChatMessage & MethodCallQueue validation
	if cErr := eng.HandleChatSend("core-wave14", authRep.SessionID, model.ChatMessage{
		MessageID: "m-bad", Audience: "all_channels_invalid", Text: "hi",
	}); cErr == nil || cErr.Code != model.ErrInvalidArguments {
		t.Fatalf("expected invalid_arguments for bad chat audience, got %v", cErr)
	}
	if _, qErr := eng.HandleQueueSubmit("core-wave14", authRep.SessionID, model.MethodCallQueue{
		QueueID: "Q-bad-trig", Epoch: 1, BasedOnRev: 1, Trigger: "auto_hack",
		Steps: []model.QueueStep{{StepIndex: 0, MethodID: "#04", Actor: "companion"}},
	}); qErr == nil || qErr.Code != model.ErrInvalidArguments {
		t.Fatalf("expected invalid_arguments for bad trigger, got %v", qErr)
	}

	// 2. Unanswered confirmation prompt timeout -> cancelled + queue.declined + refusal cooldown (§5.2 Item 2)
	proactiveSteps := []model.QueueStep{
		{StepIndex: 0, MethodID: "#05", Actor: "player", Args: map[string]any{"qty": 10}},
	}
	_, holdErr := eng.HandleQueueSubmit("core-wave14", authRep.SessionID, model.MethodCallQueue{
		QueueID: "Q-unanswered", Epoch: 1, BasedOnRev: 1, Trigger: "proactive",
		IntentSummary: "Auto-buy 10 supply", Steps: proactiveSteps,
	})
	if holdErr == nil || holdErr.Code != model.ErrConfirmationRequired {
		t.Fatalf("expected confirmation_required, got %v", holdErr)
	}
	advanceClock(600) // Exceeds ConfirmPromptTimeoutMs (500ms)

	// Attempting to submit a new proactive queue with the same target fingerprint lazily reaps Q-unanswered into cooldown!
	_, cdErr := eng.HandleQueueSubmit("core-wave14", authRep.SessionID, model.MethodCallQueue{
		QueueID: "Q-repeat-spam", Epoch: 1, BasedOnRev: 1, Trigger: "proactive",
		IntentSummary: "Spam prompt again", Steps: proactiveSteps,
	})
	if cdErr == nil || cdErr.Code != model.ErrResourceLimit || cdErr.CooldownMs == nil {
		t.Fatalf("expected 429 resource_limit refusal cooldown after prompt timeout, got %v", cdErr)
	}
	qExpired, _ := eng.HandleQueueQuery("core-wave14", authRep.SessionID, "Q-unanswered")
	if qExpired.QueueState != "cancelled" || *qExpired.StepReceipts[0].FailureReason != "confirmation_timed_out" {
		t.Fatalf("expected Q-unanswered cancelled with confirmation_timed_out, got %+v", qExpired)
	}
	select {
	case ev := <-evCh:
		if ev.Event != "queue.declined" {
			t.Fatalf("expected queue.declined push event on prompt timeout, got %s", ev.Event)
		}
	default:
		t.Fatal("expected queue.declined event on push stream")
	}

	// 3. Quiet-period adaptive wakeup push: silent within backoff, wakes once elapsed (§3.4 & Pseudocode §3.3)
	// Sync at +10ms (still < backoff) -> reset baseline with a significant sync first:
	base1 := int64(1)
	_ = eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-wave14", Epoch: 1, Rev: 2, BaseRev: &base1, Mode: "delta", SignificantEvent: true,
		StateBrief: map[string]any{"tick": 2},
	})
	<-evCh // Drain significant context.push

	advanceClock(20) // 20ms < 100ms MinSyncIntervalMs -> quiet delta MUST NOT push!
	base2 := int64(2)
	_ = eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-wave14", Epoch: 1, Rev: 3, BaseRev: &base2, Mode: "delta", SignificantEvent: false,
		StateBrief: map[string]any{"tick": 3},
	})
	select {
	case unexpected := <-evCh:
		t.Fatalf("expected zero push during quiet window, got %+v", unexpected)
	default:
	}

	advanceClock(200) // Now elapsed >= doubled backoff (200ms) -> quiet delta MUST emit throttled wakeup push!
	base3 := int64(3)
	_ = eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-wave14", Epoch: 1, Rev: 4, BaseRev: &base3, Mode: "delta", SignificantEvent: false,
		StateBrief: map[string]any{"tick": 4},
	})
	select {
	case wakeEv := <-evCh:
		if wakeEv.Event != "context.push" {
			t.Fatalf("expected throttled context.push wakeup, got %s", wakeEv.Event)
		}
	default:
		t.Fatal("expected throttled quiet-period context.push once IdleBackoffMs elapsed")
	}

	// 4. Sustained-step timeout MUST dispatch origin.execute(kind="cancel") to halt zombie Origin tasks
	fo.handler = func(req model.OriginExecuteRequest) model.OriginExecuteReply {
		if req.Kind == "invoke" && req.MethodID != nil && *req.MethodID == "#01" {
			go func() {
				time.Sleep(15 * time.Millisecond)
				advanceClock(100) // Trigger SustainedStepTimeoutMs (60ms)
			}()
			return model.OriginExecuteReply{ExecutionID: req.ExecutionID, State: "executing"}
		}
		return model.OriginExecuteReply{ExecutionID: req.ExecutionID, State: "succeeded"}
	}
	base4 := int64(4)
	_ = eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-wave14", Epoch: 1, Rev: 5, BaseRev: &base4, Mode: "delta", SignificantEvent: true,
		StateBrief: map[string]any{"tick": 5},
	})
	<-evCh
	timedOutReceipt, gErr := eng.HandleQueueSubmit("core-wave14", authRep.SessionID, model.MethodCallQueue{
		QueueID: "Q-sustained-timeout", Epoch: 1, BasedOnRev: 5, Trigger: "player_command",
		Steps: []model.QueueStep{
			{StepIndex: 0, MethodID: "#01", Actor: "companion", Args: map[string]any{}},
			{StepIndex: 1, MethodID: "#04", Actor: "companion", Args: map[string]any{}},
		},
	})
	if gErr != nil || timedOutReceipt.QueueState != "aborted" {
		t.Fatalf("expected aborted receipt on sustained timeout, got %+v err=%v", timedOutReceipt, gErr)
	}
	if timedOutReceipt.StepReceipts[0].State != "failed" || timedOutReceipt.StepReceipts[1].State != "aborted" {
		t.Fatalf("expected step 0 failed and step 1 aborted, got %+v", timedOutReceipt.StepReceipts)
	}
	sawOriginCancel := false
	fo.mu.Lock()
	for _, call := range fo.calls {
		if call.Kind == "cancel" {
			sawOriginCancel = true
		}
	}
	fo.mu.Unlock()
	if !sawOriginCancel {
		t.Fatal("expected Hub to send origin.execute(kind='cancel') when sustained step timed out")
	}

	// 5. Dynamic mid-session capability update: narrows removed #04, updates digest, NEVER auto-grants new #99 (§3.3 & §4.1)
	updatedCaps := []model.CapabilityEntry{
		caps[0], // #01
		caps[2], // #05
		{
			MethodID: "#99", Name: "admin_nuke", Description: "New unlocked capability not in frozen scope",
			PermittedActors: []string{"companion"},
			ParamsSchema:    map[string]any{"type": "object"},
			DefaultPolicy:   "allow",
		},
	}
	newCapDigest, _ := jcs.DigestSHA256("g2a.cap.v1", updatedCaps)
	if gErr := eng.HandleOriginCapabilities(model.OriginCapabilitiesPayload{
		SlotID: "slot-wave14", RoleKind: "companion", BillingMode: "player_metered",
		PlayerID: "player-A", CapabilityTableDigest: newCapDigest, Capabilities: updatedCaps, OriginExecuteURL: "mem://wave14",
	}); gErr != nil {
		t.Fatalf("dynamic capabilities err: %v", gErr)
	}
	select {
	case capEv := <-evCh:
		if capEv.Event != "capabilities.updated" {
			t.Fatalf("expected capabilities.updated event, got %s", capEv.Event)
		}
	default:
		t.Fatal("expected capabilities.updated event on dynamic capability publish")
	}
	base5 := int64(5)
	_ = eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-wave14", Epoch: 1, Rev: 6, BaseRev: &base5, Mode: "delta", SignificantEvent: true,
		StateBrief: map[string]any{"tick": 6},
		UpsertedMethods: []model.ActiveMethodGrant{
			{MethodID: "#99", Actor: "companion", Readiness: "ready"},
		},
	})
	<-evCh
	if _, escErr := eng.HandleQueueSubmit("core-wave14", authRep.SessionID, model.MethodCallQueue{
		QueueID: "Q-escalate-99", Epoch: 1, BasedOnRev: 6, Trigger: "player_command",
		Steps: []model.QueueStep{{StepIndex: 0, MethodID: "#99", Actor: "companion"}},
	}); escErr == nil || escErr.Code != model.ErrPermissionDenied {
		t.Fatalf("expected permission_denied on unapproved newly added #99 capability, got %v", escErr)
	}

	// 6. Multiplayer player_metered independent_npc privacy-isolated host handoff (§4.3)
	npcCaps := []model.CapabilityEntry{
		{
			MethodID: "#10", Name: "command_boss", Description: "Boss move",
			PermittedActors: []string{"npc_boss"},
			ParamsSchema:    map[string]any{"type": "object"},
			DefaultPolicy:   "allow",
		},
	}
	npcDigest, _ := jcs.DigestSHA256("g2a.cap.v1", npcCaps)
	_ = eng.HandleOriginCapabilities(model.OriginCapabilitiesPayload{
		SlotID: "slot-npc-boss", RoleKind: "independent_npc", BillingMode: "player_metered",
		PlayerID: "host-player-1", RoomID: "room-raid-1", CapabilityTableDigest: npcDigest,
		Capabilities: npcCaps, OriginExecuteURL: "mem://npc",
	})
	_ = eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-npc-boss", Epoch: 1, Rev: 1, Mode: "full", SignificantEvent: true,
		StateBrief: map[string]any{"phase": 1},
		UpsertedMethods: []model.ActiveMethodGrant{
			{MethodID: "#10", Actor: "npc_boss", Readiness: "ready"},
		},
	})
	npcAuth1, gErr := eng.HandleSessionAuthorize("0.0.1", "core-host-1", model.AuthorizeRequest{
		SlotID: "slot-npc-boss", PlayerID: "host-player-1",
	})
	if gErr != nil {
		t.Fatalf("npc host-1 authorize: %v", gErr)
	}

	// Handoff to teammate host-player-2 -> closes host-1 session immediately & rebinds slot to host-player-2!
	if hErr := eng.HandleOriginPlayerEvent(model.OriginPlayerEvent{
		SlotID: "slot-npc-boss", Kind: "host_handoff", NewPlayerID: "host-player-2",
	}); hErr != nil {
		t.Fatalf("host_handoff failed: %v", hErr)
	}
	if _, hbErr := eng.HandleSessionHeartbeat("core-host-1", npcAuth1.SessionID); hbErr == nil || hbErr.Code != model.ErrRevoked {
		t.Fatalf("expected previous host session to be revoked after handoff, got %v", hbErr)
	}
	npcAuth2, gErr := eng.HandleSessionAuthorize("0.0.1", "core-host-2", model.AuthorizeRequest{
		SlotID: "slot-npc-boss", PlayerID: "host-player-2",
	})
	if gErr != nil || npcAuth2.FrozenScope.PlayerID != "host-player-2" {
		t.Fatalf("expected host-player-2 to authorize cleanly onto handed-off NPC slot, got %+v err=%v", npcAuth2, gErr)
	}
}

func TestSlotGovernanceFRANDSharedCooldownAndExtensionProjection(t *testing.T) {
	nowMs := int64(500000)
	p, err := store.NewPersistencePort("")
	if err != nil {
		t.Fatal(err)
	}
	cfg := hub.DefaultConfig()
	cfg.AutoApproveScope = true
	cfg.ClockNowMs = func() int64 { return nowMs }
	eng := hub.NewEngine(cfg, p, &fakeOrigin{}, nil, nil)

	caps := []model.CapabilityEntry{
		{
			MethodID: "#01", Name: "ping_map", Description: "Tactical ping",
			PermittedActors: []string{"companion"},
			ParamsSchema:    map[string]any{"type": "object"},
			DefaultPolicy:   "allow",
		},
	}
	capDigest, _ := jcs.DigestSHA256("g2a.cap.v1", caps)

	// 1. FRAND transparency (§7.3): companion allowlist without public allowlist_criteria_url MUST be rejected!
	if fErr := eng.HandleOriginCapabilities(model.OriginCapabilitiesPayload{
		SlotID:                "slot-frand",
		RoleKind:              "companion",
		BillingMode:           "player_metered",
		CapabilityTableDigest: capDigest,
		Capabilities:          caps,
		AllowlistedCores:      []string{"core-first-party", "core-open-third-party"},
	}); fErr == nil || fErr.Code != model.ErrInvalidArguments {
		t.Fatalf("expected invalid_arguments when companion allowlist omits allowlist_criteria_url, got %v", fErr)
	}

	// Publishing with objective FRAND criteria URL + SharedCooldownMs=400ms + MaxSessions=2 succeeds
	if fErr := eng.HandleOriginCapabilities(model.OriginCapabilitiesPayload{
		SlotID:                "slot-frand",
		RoleKind:              "companion",
		BillingMode:           "player_metered",
		CapabilityTableDigest: capDigest,
		Capabilities:          caps,
		OriginExecuteURL:      "mem://frand",
		MaxSessions:           2,
		SharedCooldownMs:      400,
		AllowlistedCores:      []string{"core-first-party", "core-open-third-party", "core-overflow"},
		AllowlistCriteriaURL:  "https://game.example/g2a-frand-criteria",
	}); fErr != nil {
		t.Fatalf("expected valid FRAND capability publish, got %v", fErr)
	}

	// 2. Sync full view with two modular extensions (§8.1)
	if sErr := eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-frand", Epoch: 1, Rev: 1, Mode: "full", SignificantEvent: true,
		StateBrief: map[string]any{"match": "casual"},
		UpsertedMethods: []model.ActiveMethodGrant{
			{MethodID: "#01", Actor: "companion", Readiness: "ready"},
		},
		ExtensionsData: map[string]map[string]any{
			"org.g2a.sim.batch_blueprint.v1": {"grid": "8x8", "prefab": "watchtower"},
			"org.g2a.commerce.catalog.v1":    {"items": []string{"potion"}},
		},
	}); sErr != nil {
		t.Fatalf("sync err: %v", sErr)
	}

	// Session 1 negotiates ONLY org.g2a.sim.batch_blueprint.v1; Session 2 negotiates ZERO extensions
	auth1, gErr := eng.HandleSessionAuthorize("0.0.1", "core-first-party", model.AuthorizeRequest{
		SlotID:     "slot-frand",
		Extensions: []model.ExtensionSpec{{Name: "org.g2a.sim.batch_blueprint.v1", Required: false}},
	})
	if gErr != nil {
		t.Fatalf("auth1 err: %v", gErr)
	}
	auth2, gErr := eng.HandleSessionAuthorize("0.0.1", "core-open-third-party", model.AuthorizeRequest{
		SlotID: "slot-frand",
	})
	if gErr != nil {
		t.Fatalf("auth2 err: %v", gErr)
	}
	// MaxSessions=2: a 3rd distinct Core MUST receive 429 resource_limit
	if _, maxErr := eng.HandleSessionAuthorize("0.0.1", "core-overflow", model.AuthorizeRequest{
		SlotID: "slot-frand",
	}); maxErr == nil || maxErr.Code != model.ErrResourceLimit {
		t.Fatalf("expected 429 resource_limit when exceeding MaxSessions=2, got %v", maxErr)
	}

	ctx1, _ := eng.HandleContextPull("core-first-party", auth1.SessionID)
	if ctx1.ExtensionsData == nil || ctx1.ExtensionsData["org.g2a.sim.batch_blueprint.v1"]["prefab"] != "watchtower" {
		t.Fatalf("expected negotiated batch_blueprint extension in ctx1, got %+v", ctx1.ExtensionsData)
	}
	if _, leaked := ctx1.ExtensionsData["org.g2a.commerce.catalog.v1"]; leaked {
		t.Fatal("unnegotiated commerce.catalog extension leaked into ctx1")
	}

	ctx2, _ := eng.HandleContextPull("core-open-third-party", auth2.SessionID)
	if ctx2.ExtensionsData != nil {
		t.Fatalf("expected nil ExtensionsData when session negotiated zero extensions, got %+v", ctx2.ExtensionsData)
	}

	// 3. FRAND equal SharedCooldownMs: Core-1 dispatches at t=500000 -> both Core-1 and Core-2 are equally throttled until +400ms!
	if _, q1Err := eng.HandleQueueSubmit("core-first-party", auth1.SessionID, model.MethodCallQueue{
		QueueID: "Q-fp-1", Epoch: 1, BasedOnRev: 1, Trigger: "player_command",
		Steps: []model.QueueStep{{StepIndex: 0, MethodID: "#01", Actor: "companion"}},
	}); q1Err != nil {
		t.Fatalf("Q-fp-1 failed: %v", q1Err)
	}
	nowMs += 100
	if _, fpBurstErr := eng.HandleQueueSubmit("core-first-party", auth1.SessionID, model.MethodCallQueue{
		QueueID: "Q-fp-2", Epoch: 1, BasedOnRev: 1, Trigger: "player_command",
		Steps: []model.QueueStep{{StepIndex: 0, MethodID: "#01", Actor: "companion"}},
	}); fpBurstErr == nil || fpBurstErr.Code != model.ErrResourceLimit {
		t.Fatalf("first-party Core MUST obey equal FRAND SharedCooldownMs, got %v", fpBurstErr)
	}
	nowMs += 350 // Total +450ms >= 400ms -> third-party Core succeeds!
	if _, q3Err := eng.HandleQueueSubmit("core-open-third-party", auth2.SessionID, model.MethodCallQueue{
		QueueID: "Q-tp-1", Epoch: 1, BasedOnRev: 1, Trigger: "player_command",
		Steps: []model.QueueStep{{StepIndex: 0, MethodID: "#01", Actor: "companion"}},
	}); q3Err != nil {
		t.Fatalf("expected third-party Core to succeed once SharedCooldownMs elapsed, got %v", q3Err)
	}

	// 4. Ranked match disables companion slot (§4.3): closes existing sessions and blocks new session.authorize!
	if dErr := eng.HandleOriginCapabilities(model.OriginCapabilitiesPayload{
		SlotID:                "slot-frand",
		RoleKind:              "companion",
		BillingMode:           "player_metered",
		CapabilityTableDigest: capDigest,
		Capabilities:          caps,
		Disabled:              true,
	}); dErr != nil {
		t.Fatalf("disable slot err: %v", dErr)
	}
	if _, hbErr := eng.HandleSessionHeartbeat("core-first-party", auth1.SessionID); hbErr == nil || hbErr.Code != model.ErrRevoked {
		t.Fatalf("expected active session to be revoked when slot transitions to disabled, got %v", hbErr)
	}
	if _, disAuthErr := eng.HandleSessionAuthorize("0.0.1", "core-first-party", model.AuthorizeRequest{
		SlotID: "slot-frand",
	}); disAuthErr == nil || disAuthErr.Code != model.ErrPermissionDenied {
		t.Fatalf("expected permission_denied when authorizing against disabled ranked-mode slot, got %v", disAuthErr)
	}
}

type flakyUnknownOrigin struct {
	mu          sync.Mutex
	seenExecIDs []string
	cancels     []string
}

func (f *flakyUnknownOrigin) Execute(_ string, req model.OriginExecuteRequest) (model.OriginExecuteReply, *model.G2AError) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if req.Kind == "cancel" {
		f.cancels = append(f.cancels, req.ExecutionID)
		return model.OriginExecuteReply{ExecutionID: req.ExecutionID, State: "cancelled"}, nil
	}
	f.seenExecIDs = append(f.seenExecIDs, req.ExecutionID)
	if len(f.seenExecIDs) == 1 {
		// Simulate HTTP timeout after Origin already cached execution_id in its idempotency ledger!
		return model.OriginExecuteReply{}, model.NewError(model.ErrOutcomeUnknown, 504, "Simulated Origin gateway timeout")
	}
	return model.OriginExecuteReply{
		ExecutionID:   req.ExecutionID,
		State:         "succeeded",
		EffectSummary: map[string]any{"reconciled_from_ledger": true},
	}, nil
}

func TestOutcomeUnknownIdempotentReconciliationAndEpochPromptCleanup(t *testing.T) {
	p, err := store.NewPersistencePort("")
	if err != nil {
		t.Fatal(err)
	}
	cfg := hub.DefaultConfig()
	cfg.AutoApproveScope = true
	fo := &flakyUnknownOrigin{}
	eng := hub.NewEngine(cfg, p, fo, nil, nil)

	caps := []model.CapabilityEntry{
		{
			MethodID: "#01", Name: "toggle_valve", Description: "Toggle valve",
			PermittedActors: []string{"companion"},
			ParamsSchema:    map[string]any{"type": "object"},
			DefaultPolicy:   "allow",
		},
		{
			MethodID: "#02", Name: "overload_reactor", Description: "Confirm action",
			PermittedActors: []string{"companion"},
			ParamsSchema:    map[string]any{"type": "object"},
			DefaultPolicy:   "confirm",
		},
	}
	capDigest, _ := jcs.DigestSHA256("g2a.cap.v1", caps)
	_ = eng.HandleOriginCapabilities(model.OriginCapabilitiesPayload{
		SlotID: "slot-w22", RoleKind: "companion", BillingMode: "player_metered",
		CapabilityTableDigest: capDigest, Capabilities: caps, OriginExecuteURL: "mem://w22",
	})
	_ = eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-w22", Epoch: 1, Rev: 2, Mode: "full", SignificantEvent: true,
		StateBrief: map[string]any{"valve": "closed"},
		UpsertedMethods: []model.ActiveMethodGrant{
			{MethodID: "#01", Actor: "companion", Readiness: "ready"},
			{MethodID: "#02", Actor: "companion", Readiness: "ready"},
		},
	})

	// 1. Non-monotonic delta rev <= base_rev MUST be rejected with 400 invalid_arguments
	base2 := int64(2)
	if badDeltaErr := eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-w22", Epoch: 1, Rev: 2, BaseRev: &base2, Mode: "delta",
		StateBrief: map[string]any{"valve": "closed"},
	}); badDeltaErr == nil || badDeltaErr.Code != model.ErrInvalidArguments {
		t.Fatalf("expected non-monotonic delta rev<=base_rev rejection, got %v", badDeltaErr)
	}

	auth, gErr := eng.HandleSessionAuthorize("0.0.1", "core-w22", model.AuthorizeRequest{SlotID: "slot-w22"})
	if gErr != nil {
		t.Fatal(gErr)
	}

	// 2. 504 outcome_unknown on attempt 1 MUST reconcile with the EXACT SAME execution_id on attempt 2!
	rec, qErr := eng.HandleQueueSubmit("core-w22", auth.SessionID, model.MethodCallQueue{
		QueueID: "Q-w22-reconcile", Epoch: 1, BasedOnRev: 2, Trigger: "player_command",
		Steps: []model.QueueStep{{StepIndex: 0, MethodID: "#01", Actor: "companion"}},
	})
	if qErr != nil || rec.QueueState != "completed" {
		t.Fatalf("expected 504 outcome_unknown idempotent reconciliation to succeed, rec=%+v err=%v", rec, qErr)
	}
	if len(fo.seenExecIDs) != 2 || fo.seenExecIDs[0] != fo.seenExecIDs[1] {
		t.Fatalf("expected exact same execution_id across 504 reconciliation, got %+v", fo.seenExecIDs)
	}

	// 3. Submit confirm-gated queue at Epoch=1 -> advance to Epoch=2 -> stale prompt MUST be popped from ConsentBroker!
	_, cErr := eng.HandleQueueSubmit("core-w22", auth.SessionID, model.MethodCallQueue{
		QueueID: "Q-w22-ep1-hold", Epoch: 1, BasedOnRev: 2, Trigger: "player_command",
		Steps: []model.QueueStep{{StepIndex: 0, MethodID: "#02", Actor: "companion"}},
	})
	if cErr == nil || cErr.Code != model.ErrConfirmationRequired {
		t.Fatalf("expected confirmation_required, got %v", cErr)
	}
	if pending := eng.Consent().SnapshotPending()["queue_prompts"].([]hub.PendingQueuePrompt); len(pending) != 1 {
		t.Fatalf("expected 1 pending prompt at epoch 1, got %d", len(pending))
	}
	if epErr := eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID: "slot-w22", Epoch: 2, Rev: 1, Mode: "full", SignificantEvent: true,
		StateBrief: map[string]any{"reloaded": true},
		UpsertedMethods: []model.ActiveMethodGrant{
			{MethodID: "#01", Actor: "companion", Readiness: "ready"},
		},
	}); epErr != nil {
		t.Fatalf("epoch advance sync failed: %v", epErr)
	}
	if pendingAfter := eng.Consent().SnapshotPending()["queue_prompts"].([]hub.PendingQueuePrompt); len(pendingAfter) != 0 {
		t.Fatalf("expected stale epoch-1 confirmation prompt to be popped on epoch advance, got %d", len(pendingAfter))
	}
}
