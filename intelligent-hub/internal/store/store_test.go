package store_test

import (
	"fmt"
	"testing"

	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/model"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/store"
)

func TestAtomicCloneRollbackFidelity(t *testing.T) {
	p, err := store.NewPersistencePort("")
	if err != nil {
		t.Fatalf("NewPersistencePort: %v", err)
	}
	defer func() { _ = p.Close() }()

	capVal := int64(10)
	origQueueID := "Q-orig"
	_, gErr := store.Atomic(p, func(d *store.HubStoreData) (struct{}, *model.G2AError) {
		d.Slots["slot-1"] = &store.SlotRecord{
			SlotID:           "slot-1",
			RoleKind:         "companion",
			PlayerOnline:     true,
			Capabilities:     []model.CapabilityEntry{{MethodID: "#01", Name: "move"}},
			CapabilityByID:   map[string]model.CapabilityEntry{"#01": {MethodID: "#01", Name: "move"}},
			AllowlistedCores: map[string]bool{"core-1": true},
		}
		d.SlotViews["slot-1"] = &store.SlotView{
			SlotID:     "slot-1",
			Epoch:      1,
			Rev:        5,
			StateBrief: map[string]any{"hp": 100},
			Methods: map[string]model.ActiveMethodGrant{
				"#01|companion": {
					MethodID:  "#01",
					Actor:     "companion",
					Readiness: "ready",
					Targets:   []model.TargetCandidate{{TargetID: "chest_A", Label: "Chest A", Readiness: "ready"}},
				},
			},
			SettledExecutions: map[string]model.OriginExecuteReply{},
		}
		d.RegisterSession(&store.SessionRecord{
			SessionID:      "sess-1",
			CallerCoreID:   "core-1",
			State:          "active",
			PlayerOnline:   true,
			ActiveQueueID:  &origQueueID,
			ConsumptionCap: &capVal,
			Scope: model.FrozenScope{
				SlotID:           "slot-1",
				PermittedMethods: map[string][]string{"#01": {"companion"}},
			},
		})
		d.RegisterQueue("sess-1|Q-orig", &store.QueueLedgerRecord{
			SessionID: "sess-1",
			QueueID:   "Q-orig",
			State:     "running",
			Queue: model.MethodCallQueue{
				QueueID: "Q-orig",
				Steps:   []model.QueueStep{{StepIndex: 0, MethodID: "#01", Actor: "companion"}},
			},
			StepReceipts: []model.StepReceipt{{StepIndex: 0, MethodID: "#01", Actor: "companion", State: "executing"}},
		})
		d.PolicyOverrides["slot-1|#01|companion"] = "allow"
		d.AppendAudit(1000, "test.init", "slot-1", "sess-1", "Q-orig", "initialized")
		return struct{}{}, nil
	})
	if gErr != nil {
		t.Fatalf("seed Atomic failed: %v", gErr)
	}

	// Mutate nested slices, maps, and pointers, then return an error to trigger Clone() rollback.
	_, failErr := store.Atomic(p, func(d *store.HubStoreData) (struct{}, *model.G2AError) {
		d.Slots["slot-1"].PlayerOnline = false
		d.Slots["slot-1"].Capabilities[0].Name = "corrupted"
		d.SlotViews["slot-1"].Rev = 999
		d.SlotViews["slot-1"].StateBrief["hp"] = 0
		mg := d.SlotViews["slot-1"].Methods["#01|companion"]
		mg.Targets[0].Label = "Corrupted Chest"
		d.Sessions["sess-1"].State = "closed"
		*d.Sessions["sess-1"].ActiveQueueID = "Q-mutated"
		d.Sessions["sess-1"].Scope.PermittedMethods["#01"][0] = "corrupted_actor"
		d.Queues["sess-1|Q-orig"].StepReceipts[0].State = "failed"
		d.PolicyOverrides["slot-1|#01|companion"] = "prohibit"
		d.AppendAudit(2000, "test.corrupt", "slot-1", "sess-1", "Q-orig", "should roll back")
		return struct{}{}, model.NewError(model.ErrConflict, 409, "rollback expected")
	})
	if failErr == nil || failErr.Code != model.ErrConflict {
		t.Fatalf("expected ErrConflict, got %v", failErr)
	}

	_, _ = store.ReadOnly(p, func(d *store.HubStoreData) (struct{}, *model.G2AError) {
		if !d.Slots["slot-1"].PlayerOnline || d.Slots["slot-1"].Capabilities[0].Name != "move" {
			t.Fatal("SlotRecord was not cleanly rolled back")
		}
		if d.SlotViews["slot-1"].Rev != 5 || d.SlotViews["slot-1"].StateBrief["hp"] != 100 {
			t.Fatal("SlotView scalar/map was not cleanly rolled back")
		}
		if d.SlotViews["slot-1"].Methods["#01|companion"].Targets[0].Label != "Chest A" {
			t.Fatal("SlotView nested target slice was mutated across rollback")
		}
		if d.Sessions["sess-1"].State != "active" || *d.Sessions["sess-1"].ActiveQueueID != "Q-orig" {
			t.Fatal("SessionRecord state/pointer was mutated across rollback")
		}
		if d.Sessions["sess-1"].Scope.PermittedMethods["#01"][0] != "companion" {
			t.Fatal("SessionRecord PermittedMethods slice was mutated across rollback")
		}
		if d.Queues["sess-1|Q-orig"].StepReceipts[0].State != "executing" {
			t.Fatal("QueueLedgerRecord StepReceipts was mutated across rollback")
		}
		if d.PolicyOverrides["slot-1|#01|companion"] != "allow" || len(d.AuditLog) != 1 {
			t.Fatal("PolicyOverrides or AuditLog was not rolled back")
		}
		return struct{}{}, nil
	})
}

func TestPruneClosedSessionsBoundsMemoryWhilePreservingActiveAndLiveQueues(t *testing.T) {
	d := store.NewHubStoreData()
	for i := 0; i < store.MaxClosedSessionsStored+80; i++ {
		sid := fmt.Sprintf("sess-closed-%04d", i)
		d.RegisterSession(&store.SessionRecord{
			SessionID: sid,
			State:     "closed",
		})
	}
	// Protected sessions: 1 active, 1 disconnected_grace, 1 closed with awaiting_confirmation queue
	d.RegisterSession(&store.SessionRecord{SessionID: "sess-active", State: "active"})
	d.RegisterSession(&store.SessionRecord{SessionID: "sess-grace", State: "disconnected_grace"})
	d.Sessions["sess-closed-with-queue"] = &store.SessionRecord{SessionID: "sess-closed-with-queue", State: "closed"}
	d.SessionOrder = append(d.SessionOrder, "sess-closed-with-queue")
	d.Queues["sess-closed-with-queue|Q-1"] = &store.QueueLedgerRecord{
		SessionID: "sess-closed-with-queue",
		QueueID:   "Q-1",
		State:     "awaiting_confirmation",
	}

	d.PruneClosedSessions()

	wantTotal := store.MaxClosedSessionsStored + 3
	if len(d.Sessions) != wantTotal {
		t.Fatalf("expected %d retained sessions after pruning, got %d", wantTotal, len(d.Sessions))
	}
	if _, evicted := d.Sessions["sess-closed-0000"]; evicted {
		t.Fatal("expected oldest closed session sess-closed-0000 to be evicted")
	}
	for _, keep := range []string{"sess-active", "sess-grace", "sess-closed-with-queue"} {
		if _, ok := d.Sessions[keep]; !ok {
			t.Fatalf("protected session %s was mistakenly evicted", keep)
		}
	}
}

func BenchmarkStoreAtomicInMemory(b *testing.B) {
	p, err := store.NewPersistencePort("")
	if err != nil {
		b.Fatal(err)
	}
	defer func() { _ = p.Close() }()

	_, _ = store.Atomic(p, func(d *store.HubStoreData) (struct{}, *model.G2AError) {
		d.Slots["slot-bench"] = &store.SlotRecord{
			SlotID:           "slot-bench",
			Capabilities:     []model.CapabilityEntry{{MethodID: "#01", Name: "move"}},
			CapabilityByID:   map[string]model.CapabilityEntry{"#01": {MethodID: "#01", Name: "move"}},
			AllowlistedCores: map[string]bool{},
		}
		d.SlotViews["slot-bench"] = &store.SlotView{
			SlotID:            "slot-bench",
			Epoch:             1,
			Rev:               10,
			StateBrief:        map[string]any{"hp": 100, "stamina": 85},
			Methods:           map[string]model.ActiveMethodGrant{"#01|companion": {MethodID: "#01", Actor: "companion", Readiness: "ready"}},
			SettledExecutions: map[string]model.OriginExecuteReply{},
		}
		d.RegisterSession(&store.SessionRecord{
			SessionID: "sess-bench",
			State:     "active",
			Scope:     model.FrozenScope{SlotID: "slot-bench", PermittedMethods: map[string][]string{"#01": {"companion"}}},
		})
		return struct{}{}, nil
	})

	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_, gErr := store.Atomic(p, func(d *store.HubStoreData) (int64, *model.G2AError) {
			d.Sessions["sess-bench"].ConsumptionUsed++
			return d.Sessions["sess-bench"].ConsumptionUsed, nil
		})
		if gErr != nil {
			b.Fatal(gErr)
		}
	}
}

func TestSlotSessionsSecondaryIndexIsolation(t *testing.T) {
	d := store.NewHubStoreData()
	for i := 0; i < 400; i++ {
		d.RegisterSession(&store.SessionRecord{
			SessionID:    fmt.Sprintf("closed-%03d", i),
			State:        "closed",
			PlayerOnline: false,
			Scope:        model.FrozenScope{SlotID: fmt.Sprintf("slot-bg-%d", i%10)},
		})
	}
	d.RegisterSession(&store.SessionRecord{
		SessionID:    "sess-target-active",
		State:        "active",
		PlayerOnline: true,
		Scope:        model.FrozenScope{SlotID: "slot-hot"},
	})
	d.RegisterSession(&store.SessionRecord{
		SessionID:    "sess-target-paused",
		State:        "paused_awaiting_player",
		PlayerOnline: true,
		Scope:        model.FrozenScope{SlotID: "slot-hot"},
	})

	active := d.ActiveSessionsForSlot("slot-hot")
	if len(active) != 1 || active[0].SessionID != "sess-target-active" {
		t.Fatalf("expected exactly 1 active session on slot-hot, got %+v", active)
	}
	allHot := d.SessionsForSlot("slot-hot")
	if len(allHot) != 2 {
		t.Fatalf("expected 2 total sessions on slot-hot, got %d", len(allHot))
	}
	if got := d.ActiveSessionsForSlot("slot-nonexistent"); len(got) != 0 {
		t.Fatalf("expected 0 sessions on nonexistent slot, got %d", len(got))
	}
}

func BenchmarkActiveSessionsForSlot500Closed(b *testing.B) {
	d := store.NewHubStoreData()
	for i := 0; i < 500; i++ {
		d.RegisterSession(&store.SessionRecord{
			SessionID: fmt.Sprintf("sess-hist-%03d", i),
			State:     "closed",
			Scope:     model.FrozenScope{SlotID: fmt.Sprintf("slot-other-%d", i%25)},
		})
	}
	d.RegisterSession(&store.SessionRecord{
		SessionID:    "sess-live-hot",
		State:        "active",
		PlayerOnline: true,
		Scope:        model.FrozenScope{SlotID: "slot-hot-60hz"},
	})

	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		res := d.ActiveSessionsForSlot("slot-hot-60hz")
		if len(res) != 1 {
			b.Fatal("expected 1 active session")
		}
	}
}

func TestRecursiveValidateArgsAgainstSchema(t *testing.T) {
	schema := map[string]any{
		"type":                 "object",
		"required":             []any{"passcode", "ratio", "ingredients", "grid_pos"},
		"additionalProperties": false,
		"properties": map[string]any{
			"passcode": map[string]any{
				"type":      "string",
				"minLength": 4,
				"maxLength": 8,
			},
			"ratio": map[string]any{
				"type":             "number",
				"exclusiveMinimum": 0.0,
				"maximum":          1.0,
			},
			"ingredients": map[string]any{
				"type":     "array",
				"minItems": 1,
				"maxItems": 3,
				"items": map[string]any{
					"type": "string",
					"enum": []any{"herb", "crystal", "water"},
				},
			},
			"grid_pos": map[string]any{
				"type":                 "object",
				"required":             []any{"x", "y"},
				"additionalProperties": false,
				"properties": map[string]any{
					"x": map[string]any{"type": "integer", "minimum": 0, "maximum": 15},
					"y": map[string]any{"type": "integer", "minimum": 0, "maximum": 15},
				},
			},
		},
	}

	valid := map[string]any{
		"passcode":    "7391",
		"ratio":       0.5,
		"ingredients": []any{"herb", "water"},
		"grid_pos":    map[string]any{"x": 3, "y": 12},
	}
	if err := store.ValidateArgsAgainstSchema(valid, schema); err != nil {
		t.Fatalf("expected valid composite args to pass, got %v", err)
	}

	invalidCases := []struct {
		name string
		args map[string]any
	}{
		{
			name: "passcode too short",
			args: map[string]any{"passcode": "12", "ratio": 0.5, "ingredients": []any{"herb"}, "grid_pos": map[string]any{"x": 1, "y": 1}},
		},
		{
			name: "ratio not above exclusiveMinimum",
			args: map[string]any{"passcode": "1234", "ratio": 0.0, "ingredients": []any{"herb"}, "grid_pos": map[string]any{"x": 1, "y": 1}},
		},
		{
			name: "array item outside enum",
			args: map[string]any{"passcode": "1234", "ratio": 0.5, "ingredients": []any{"herb", "poison_injected"}, "grid_pos": map[string]any{"x": 1, "y": 1}},
		},
		{
			name: "array exceeds maxItems",
			args: map[string]any{"passcode": "1234", "ratio": 0.5, "ingredients": []any{"herb", "water", "crystal", "herb"}, "grid_pos": map[string]any{"x": 1, "y": 1}},
		},
		{
			name: "nested object out-of-bounds coordinate",
			args: map[string]any{"passcode": "1234", "ratio": 0.5, "ingredients": []any{"herb"}, "grid_pos": map[string]any{"x": 99, "y": 1}},
		},
		{
			name: "nested object unexpected property",
			args: map[string]any{"passcode": "1234", "ratio": 0.5, "ingredients": []any{"herb"}, "grid_pos": map[string]any{"x": 1, "y": 1, "z": 2}},
		},
	}
	for _, tc := range invalidCases {
		if err := store.ValidateArgsAgainstSchema(tc.args, schema); err == nil || err.Code != model.ErrInvalidArguments {
			t.Fatalf("case %q expected invalid_arguments, got %v", tc.name, err)
		}
	}
}
