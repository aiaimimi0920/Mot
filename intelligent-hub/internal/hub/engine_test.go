package hub_test

import (
	"math"
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
