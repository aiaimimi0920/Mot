package jcs_test

import (
	"errors"
	"math"
	"testing"

	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/jcs"
)

func TestRFC8785UTF16KeySorting(t *testing.T) {
	// 1. Mixed ASCII and Latin-1 keys: "a\u00e9" must sort BEFORE "b" ('a' == 0x61 < 'b' == 0x62).
	mixed, err := jcs.CanonicalizeUTF8(map[string]any{
		"b":      1,
		"a\u00e9": 2,
		"a":      0,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	const wantMixed = `{"a":0,"aé":2,"b":1}`
	if string(mixed) != wantMixed {
		t.Fatalf("mixed ASCII/non-ASCII order mismatch: got %s, want %s", string(mixed), wantMixed)
	}

	// 2. RFC 8785 Section 3.2.3 surrogate vs BMP ordering:
	// U+1F600 ("😀") encodes in UTF-16 as [0xD83D, 0xDE00], whereas U+E000 encodes as [0xE000].
	// In UTF-8 byte order U+E000 (0xEE...) < U+1F600 (0xF0...), but in RFC 8785 UTF-16 code unit order
	// 0xD83D < 0xE000, so "😀" MUST precede "\uE000".
	astralVsBMP, err := jcs.CanonicalizeUTF8(map[string]any{
		"\uE000": "bmp_private_use",
		"😀":      "supplementary_astral",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	wantAstral := "{\"😀\":\"supplementary_astral\",\"\uE000\":\"bmp_private_use\"}"
	if string(astralVsBMP) != wantAstral {
		t.Fatalf("RFC 8785 UTF-16 surrogate ordering failed: got %q, want %q", string(astralVsBMP), wantAstral)
	}
}

func TestStrictJSONRejectionAndFloatFormatting(t *testing.T) {
	if _, err := jcs.ParseStrictJSON([]byte(`{"outer":{"dup":1,"dup":2}}`)); !errors.Is(err, jcs.ErrDuplicateKey) {
		t.Fatalf("expected nested ErrDuplicateKey, got %v", err)
	}
	if _, err := jcs.ParseStrictJSON([]byte(`{"ok":1} {"trailing":2}`)); !errors.Is(err, jcs.ErrInvalidJSON) {
		t.Fatalf("expected trailing token rejection, got %v", err)
	}
	if err := jcs.ValidateStrictValue(map[string]any{"bad": math.Inf(1)}); !errors.Is(err, jcs.ErrNonFiniteFloat) {
		t.Fatalf("expected +Inf rejection, got %v", err)
	}

	negZero := math.Copysign(0, -1)
	raw, err := jcs.CanonicalizeUTF8(map[string]any{
		"neg_zero": negZero,
		"int_flt":  20.0,
		"small":    1e-7,
		"large":    1e21,
		"ctrl":     "a\x00\b\n\"\\z",
	})
	if err != nil {
		t.Fatalf("unexpected canonicalize error: %v", err)
	}
	const want = `{"ctrl":"a\u0000\b\n\"\\z","int_flt":20,"large":1e+21,"neg_zero":0,"small":1e-7}`
	if string(raw) != want {
		t.Fatalf("canonical float/escape mismatch:\ngot:  %s\nwant: %s", string(raw), want)
	}
}

func TestPureASTVsStructParityAndGoldenDigest(t *testing.T) {
	type stepItem struct {
		StepIndex int            `json:"step_index"`
		Args      map[string]any `json:"args"`
	}
	type confirmDoc struct {
		SessionID string     `json:"session_id"`
		QueueID   string     `json:"queue_id"`
		Steps     []stepItem `json:"steps"`
	}

	structVal := confirmDoc{
		SessionID: "sess-1",
		QueueID:   "Q-205",
		Steps: []stepItem{
			{StepIndex: 0, Args: map[string]any{"item": "timber", "qty": 20}},
		},
	}
	astVal := map[string]any{
		"queue_id":   "Q-205",
		"session_id": "sess-1",
		"steps": []any{
			map[string]any{
				"args":       map[string]any{"qty": 20.0, "item": "timber"},
				"step_index": 0,
			},
		},
	}

	dStruct, err := jcs.DigestSHA256("g2a.confirm.v1", structVal)
	if err != nil {
		t.Fatalf("struct digest failed: %v", err)
	}
	dAST, err := jcs.DigestSHA256("g2a.confirm.v1", astVal)
	if err != nil {
		t.Fatalf("AST digest failed: %v", err)
	}
	const golden = "sha256:8d8634fd72ae48e655a9151e50b06a3e1095e753cacb2c293706e59af1b9ccd8"
	if dStruct != golden || dAST != golden {
		t.Fatalf("golden digest mismatch: struct=%s ast=%s want=%s", dStruct, dAST, golden)
	}
}

func BenchmarkDigestSHA256ConfirmVector(b *testing.B) {
	astVal := map[string]any{
		"session_id": "sess-1",
		"queue_id":   "Q-205",
		"steps": []any{
			map[string]any{
				"step_index": 0,
				"args":       map[string]any{"item": "timber", "qty": 20.0},
			},
		},
	}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := jcs.DigestSHA256("g2a.confirm.v1", astVal); err != nil {
			b.Fatal(err)
		}
	}
}

func TestValidateStrictJSONBytesSecurityAndEdgeCases(t *testing.T) {
	validEnv := []byte(`{"g2a_version":"0.0.1","request_id":"req-1","timestamp_ms":1700000000,"operation":"origin.sync","payload":{"slot_id":"s1","rev":4,"emoji":"\uD83D\uDE00"},"extensions":{}}`)
	if err := jcs.ValidateStrictJSONBytes(validEnv); err != nil {
		t.Fatalf("expected valid envelope to pass, got %v", err)
	}

	// Escaped vs unescaped duplicate key smuggling ("\u0061" == "a") MUST be rejected!
	if err := jcs.ValidateStrictJSONBytes([]byte(`{"\u0061":1,"a":2}`)); !errors.Is(err, jcs.ErrDuplicateKey) {
		t.Fatalf("expected escaped duplicate key rejection, got %v", err)
	}
	// Large >12-key object duplicate check
	if err := jcs.ValidateStrictJSONBytes([]byte(`{"k0":0,"k1":1,"k2":2,"k3":3,"k4":4,"k5":5,"k6":6,"k7":7,"k8":8,"k9":9,"k10":10,"k11":11,"k12":12,"k3":99}`)); !errors.Is(err, jcs.ErrDuplicateKey) {
		t.Fatalf("expected >12-key duplicate rejection, got %v", err)
	}
	// Lone high surrogate, lone low surrogate, leading zero, & trailing token MUST fail
	for _, bad := range []string{
		`{"bad":"\uD83D"}`,
		`{"bad":"\uDE00"}`,
		`{"bad":"\uD83D\u0041"}`,
		`{"bad":01}`,
		`{"bad":"raw` + "\x01" + `ctrl"}`,
		`{"ok":1} trailing`,
	} {
		if err := jcs.ValidateStrictJSONBytes([]byte(bad)); err == nil {
			t.Fatalf("expected %q to fail strict validation", bad)
		}
	}
}

func BenchmarkValidateStrictJSONBytesEnvelope(b *testing.B) {
	raw := []byte(`{"g2a_version":"0.0.1","request_id":"req-60hz","timestamp_ms":1700000000,"operation":"origin.sync","payload":{"slot_id":"slot-1","epoch":1,"rev":42,"base_rev":41,"mode":"delta","significant_event":false,"state_brief":{"hp":100,"gold":250},"upserted_methods":[],"removed_methods":[],"settled_executions":[]},"extensions":{}}`)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if err := jcs.ValidateStrictJSONBytes(raw); err != nil {
			b.Fatal(err)
		}
	}
}
