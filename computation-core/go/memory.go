package g2acore

import (
	"sort"
	"strings"
	"sync"
	"time"
)

const MemoryBundleFormatV1 = "g2a.memory.bundle.v1"

var disclosureOrder = map[string]int{
	"private_player_only":  0,
	"same_game_companion":  1,
	"cross_game_companion": 2,
}

var validEpistemicSources = map[string]bool{
	"game_observed": true,
	"player_stated": true,
	"agent_summary": true,
	"player_edited": true,
}

var supportedPersonaKeys = map[string]bool{
	"tone":      true,
	"verbosity": true,
	"nickname":  true,
	"language":  true,
}

type MemoryRecord struct {
	MemoryID           string  `json:"memory_id"`
	CreatedAtMs        int64   `json:"created_at_ms"`
	ImportedAtMs       *int64  `json:"imported_at_ms,omitempty"`
	EpistemicSource    string  `json:"epistemic_source"`
	GameOriginID       *string `json:"game_origin_id,omitempty"`
	TimelineTag        *string `json:"timeline_tag,omitempty"`
	PinnedByPlayer     bool    `json:"pinned_by_player"`
	MaxDisclosureScope string  `json:"max_disclosure_scope"`
	Content            string  `json:"content"`
	ObservedEpoch      *int64  `json:"observed_epoch,omitempty"`
}

type SpoilerGuard struct {
	CurrentEpoch        int64
	GameOriginID        string
	AllowedTimelineTags []string
	QueryHint           string
	MaxResults          int
}

type MemoryBundleV1 struct {
	Format              string            `json:"format"`
	ExportedAtMs        int64             `json:"exported_at_ms"`
	PersonaSettings     map[string]any    `json:"persona_settings"`
	AutonomyPreferences map[string]string `json:"autonomy_preferences"`
	Records             []MemoryRecord    `json:"records"`
}

type ImportReceiptV1 struct {
	RecordStatuses      map[string]string `json:"record_statuses"` // "imported" | "transformed" | "rejected"
	AppliedSettings     []string          `json:"applied_settings"`
	UnsupportedSettings []string          `json:"unsupported_settings"`
}

type InMemoryCompanionStore struct {
	mu       sync.RWMutex
	records  map[string]map[string]MemoryRecord
	persona  map[string]map[string]any
	autonomy map[string]map[string]string
}

func NewInMemoryCompanionStore() *InMemoryCompanionStore {
	return &InMemoryCompanionStore{
		records:  make(map[string]map[string]MemoryRecord),
		persona:  make(map[string]map[string]any),
		autonomy: make(map[string]map[string]string),
	}
}

func (s *InMemoryCompanionStore) UpsertRecord(playerID string, rec MemoryRecord) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.records[playerID] == nil {
		s.records[playerID] = make(map[string]MemoryRecord)
	}
	s.records[playerID][rec.MemoryID] = rec
}

func (s *InMemoryCompanionStore) ListRecords(playerID string) []MemoryRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]MemoryRecord, 0, len(s.records[playerID]))
	for _, r := range s.records[playerID] {
		out = append(out, r)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAtMs > out[j].CreatedAtMs })
	return out
}

func (s *InMemoryCompanionStore) RecallSafe(playerID string, guard SpoilerGuard) []MemoryRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	tagSet := make(map[string]bool, len(guard.AllowedTimelineTags))
	for _, t := range guard.AllowedTimelineTags {
		tagSet[t] = true
	}
	keywords := strings.Fields(strings.ToLower(guard.QueryHint))

	type scored struct {
		rec   MemoryRecord
		score int
	}
	var cands []scored
	for _, r := range s.records[playerID] {
		if r.MaxDisclosureScope == "private_player_only" {
			continue
		}
		if r.MaxDisclosureScope == "same_game_companion" && r.GameOriginID != nil && guard.GameOriginID != "" && *r.GameOriginID != guard.GameOriginID {
			continue
		}
		if r.ObservedEpoch != nil && guard.CurrentEpoch > 0 && *r.ObservedEpoch > guard.CurrentEpoch {
			continue
		}
		if len(tagSet) > 0 && r.TimelineTag != nil && *r.TimelineTag != "" && !tagSet[*r.TimelineTag] {
			continue
		}
		low := strings.ToLower(r.Content)
		sc := 0
		if r.PinnedByPlayer {
			sc += 1000
		}
		for _, kw := range keywords {
			if len(kw) >= 2 && strings.Contains(low, kw) {
				sc += 10
			}
		}
		cands = append(cands, scored{rec: r, score: sc})
	}
	sort.Slice(cands, func(i, j int) bool {
		if cands[i].score != cands[j].score {
			return cands[i].score > cands[j].score
		}
		return cands[i].rec.CreatedAtMs > cands[j].rec.CreatedAtMs
	})
	limit := guard.MaxResults
	if limit <= 0 {
		limit = 48
	}
	if len(cands) > limit {
		cands = cands[:limit]
	}
	out := make([]MemoryRecord, len(cands))
	for i, c := range cands {
		out[i] = c.rec
	}
	return out
}

type MemoryService struct {
	Store           *InMemoryCompanionStore
	PlatformCeiling string
}

func NewMemoryService(store *InMemoryCompanionStore, platformCeiling string) *MemoryService {
	if platformCeiling == "" {
		platformCeiling = "same_game_companion"
	}
	return &MemoryService{Store: store, PlatformCeiling: platformCeiling}
}

func (m *MemoryService) ExportBundle(playerID string) MemoryBundleV1 {
	m.Store.mu.RLock()
	defer m.Store.mu.RUnlock()
	persona := make(map[string]any)
	for k, v := range m.Store.persona[playerID] {
		persona[k] = v
	}
	auto := make(map[string]string)
	for k, v := range m.Store.autonomy[playerID] {
		auto[k] = v
	}
	recs := make([]MemoryRecord, 0, len(m.Store.records[playerID]))
	for _, r := range m.Store.records[playerID] {
		recs = append(recs, r)
	}
	sort.Slice(recs, func(i, j int) bool { return recs[i].MemoryID < recs[j].MemoryID })
	return MemoryBundleV1{
		Format:              MemoryBundleFormatV1,
		ExportedAtMs:        time.Now().UnixMilli(),
		PersonaSettings:     persona,
		AutonomyPreferences: auto,
		Records:             recs,
	}
}

func (m *MemoryService) ImportBundle(playerID string, bundle MemoryBundleV1, selectedIDs []string) (*ImportReceiptV1, error) {
	if bundle.Format != MemoryBundleFormatV1 {
		return nil, &G2AError{Code: "invalid_arguments", HTTPStatus: 400, Message: "Unsupported memory bundle format"}
	}
	sel := make(map[string]bool, len(selectedIDs))
	for _, id := range selectedIDs {
		sel[id] = true
	}
	receipt := &ImportReceiptV1{
		RecordStatuses:      make(map[string]string),
		AppliedSettings:     []string{},
		UnsupportedSettings: []string{},
	}

	m.Store.mu.Lock()
	defer m.Store.mu.Unlock()
	if m.Store.records[playerID] == nil {
		m.Store.records[playerID] = make(map[string]MemoryRecord)
	}
	if m.Store.persona[playerID] == nil {
		m.Store.persona[playerID] = make(map[string]any)
	}
	if m.Store.autonomy[playerID] == nil {
		m.Store.autonomy[playerID] = make(map[string]string)
	}

	for k, v := range bundle.PersonaSettings {
		if supportedPersonaKeys[k] {
			m.Store.persona[playerID][k] = v
			receipt.AppliedSettings = append(receipt.AppliedSettings, k)
		} else {
			receipt.UnsupportedSettings = append(receipt.UnsupportedSettings, k)
		}
	}
	for k, v := range bundle.AutonomyPreferences {
		if v == "confirm" || v == "prohibit" {
			m.Store.autonomy[playerID][k] = v
		} else {
			receipt.UnsupportedSettings = append(receipt.UnsupportedSettings, "autonomy:"+k)
		}
	}
	sort.Strings(receipt.AppliedSettings)
	sort.Strings(receipt.UnsupportedSettings)

	ceilRank := disclosureOrder[m.PlatformCeiling]
	now := time.Now().UnixMilli()
	for _, rec := range bundle.Records {
		if len(sel) > 0 && !sel[rec.MemoryID] {
			continue
		}
		if strings.TrimSpace(rec.Content) == "" || !validEpistemicSources[rec.EpistemicSource] {
			receipt.RecordStatuses[rec.MemoryID] = "rejected"
			continue
		}
		if existing, exists := m.Store.records[playerID][rec.MemoryID]; exists {
			if existing.PinnedByPlayer || existing.EpistemicSource == "player_edited" {
				receipt.RecordStatuses[rec.MemoryID] = "rejected"
				continue
			}
		}
		status := "imported"
		recRank, ok := disclosureOrder[rec.MaxDisclosureScope]
		if !ok {
			receipt.RecordStatuses[rec.MemoryID] = "rejected"
			continue
		}
		if recRank > ceilRank {
			rec.MaxDisclosureScope = m.PlatformCeiling
			status = "transformed"
		}
		impAt := now
		rec.ImportedAtMs = &impAt
		m.Store.records[playerID][rec.MemoryID] = rec
		receipt.RecordStatuses[rec.MemoryID] = status
	}
	return receipt, nil
}
