package server_test

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/hub"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/jcs"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/model"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/server"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/store"
)

func newTestHubEngine(t *testing.T) *hub.Engine {
	t.Helper()
	p, err := store.NewPersistencePort(filepath.Join(t.TempDir(), "hub_ledger.json"))
	if err != nil {
		t.Fatalf("NewPersistencePort: %v", err)
	}
	cfg := hub.DefaultConfig()
	cfg.AutoApproveScope = true
	return hub.NewEngine(cfg, p, nil, nil, nil)
}

func registerSlotForServerTest(t *testing.T, eng *hub.Engine, slotID string) {
	t.Helper()
	caps := []model.CapabilityEntry{
		{
			MethodID:        "#01",
			Name:            "harvest",
			Description:     "Harvest nearby resource",
			PermittedActors: []string{"companion"},
			ParamsSchema:    map[string]any{"type": "object"},
			Sustained:       false,
			DefaultPolicy:   "allow",
		},
	}
	capDigest, err := jcs.DigestSHA256("g2a.cap.v1", caps)
	if err != nil {
		t.Fatalf("DigestSHA256: %v", err)
	}
	if gErr := eng.HandleOriginCapabilities(model.OriginCapabilitiesPayload{
		SlotID:                slotID,
		RoleKind:              "companion",
		BillingMode:           "player_metered",
		OriginExecuteURL:      "pull://origin.execute",
		CapabilityTableDigest: capDigest,
		Capabilities:          caps,
	}); gErr != nil {
		t.Fatalf("HandleOriginCapabilities(%s): %v", slotID, gErr)
	}
	if gErr := eng.HandleOriginSync(model.OriginSyncFrame{
		SlotID:           slotID,
		Epoch:            1,
		Rev:              1,
		Mode:             "full",
		SignificantEvent: true,
		StateBrief:       map[string]any{"zone": "meadow"},
		UpsertedMethods: []model.ActiveMethodGrant{
			{MethodID: "#01", Actor: "companion", Readiness: "ready"},
		},
	}); gErr != nil {
		t.Fatalf("HandleOriginSync(%s): %v", slotID, gErr)
	}
}

func TestTrustedLoopbackHostGuardAndZeroCORS(t *testing.T) {
	eng := newTestHubEngine(t)
	h := server.NewTrustedConsentHandler(eng, server.AuthRegistry{
		ConsentToken: "trusted-secret",
	})

	for _, allowedHost := range []string{"127.0.0.1:19230", "localhost:19230", "[::1]:19230"} {
		req := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:19230/trusted/status", nil)
		req.Host = allowedHost
		req.Header.Set("Authorization", "Bearer trusted-secret")
		req.Header.Set("Origin", "https://evil.example")
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("host %s expected 200, got %d", allowedHost, rec.Code)
		}
		if rec.Header().Get("Access-Control-Allow-Origin") != "" {
			t.Fatalf("Trusted Consent Dashboard must never emit CORS headers, got %q", rec.Header().Get("Access-Control-Allow-Origin"))
		}
		if rec.Header().Get("X-Frame-Options") != "DENY" {
			t.Fatalf("expected X-Frame-Options: DENY, got %q", rec.Header().Get("X-Frame-Options"))
		}
	}

	rebindReq := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:19230/trusted/status", nil)
	rebindReq.Host = "attacker.rebind.example:19230"
	rebindReq.Header.Set("Authorization", "Bearer trusted-secret")
	rebindRec := httptest.NewRecorder()
	h.ServeHTTP(rebindRec, rebindReq)
	if rebindRec.Code != http.StatusForbidden {
		t.Fatalf("DNS-rebinding Host must return 403 Forbidden, got %d", rebindRec.Code)
	}
}

func TestConcurrentTrustedStatusAndPolicyOverrideMutations(t *testing.T) {
	eng := newTestHubEngine(t)
	// Register in reverse lexical order to verify deterministic output sorting ("slot-a" before "slot-b").
	registerSlotForServerTest(t, eng, "slot-b")
	registerSlotForServerTest(t, eng, "slot-a")

	if _, gErr := eng.HandleSessionAuthorize(model.WireVersion, "core-alpha", model.AuthorizeRequest{
		SlotID:         "slot-b",
		RequestedScope: model.RequestedScopeSpec{AllowChat: true},
	}); gErr != nil {
		t.Fatalf("authorize slot-b: %v", gErr)
	}
	if _, gErr := eng.HandleSessionAuthorize(model.WireVersion, "core-beta", model.AuthorizeRequest{
		SlotID:         "slot-a",
		RequestedScope: model.RequestedScopeSpec{AllowChat: true},
	}); gErr != nil {
		t.Fatalf("authorize slot-a: %v", gErr)
	}

	h := server.NewTrustedConsentHandler(eng, server.AuthRegistry{
		ConsentToken: "secret-consent",
	})

	policies := []string{"allow", "confirm", "prohibit"}
	slots := []string{"slot-a", "slot-b"}

	var wg sync.WaitGroup
	errCh := make(chan error, 32)

	// 8 concurrent writers mutating PolicyOverrides
	for w := 0; w < 8; w++ {
		workerID := w
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 60; i++ {
				body, _ := json.Marshal(map[string]any{
					"slot_id":   slots[(workerID+i)%len(slots)],
					"method_id": "#01",
					"actor":     "companion",
					"policy":    policies[(workerID+i)%len(policies)],
				})
				req := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:19230/trusted/policy-override", bytes.NewReader(body))
				req.Host = "127.0.0.1:19230"
				req.Header.Set("Authorization", "Bearer secret-consent")
				rec := httptest.NewRecorder()
				h.ServeHTTP(rec, req)
				if rec.Code != http.StatusOK {
					errCh <- io.ErrUnexpectedEOF
					return
				}
			}
		}()
	}

	// 8 concurrent readers snapshotting /trusted/status & /trusted/pending and serializing JSON
	for r := 0; r < 8; r++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 60; i++ {
				req := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:19230/trusted/status", nil)
				req.Host = "127.0.0.1:19230"
				req.Header.Set("Authorization", "Bearer secret-consent")
				rec := httptest.NewRecorder()
				h.ServeHTTP(rec, req)
				if rec.Code != http.StatusOK {
					errCh <- io.ErrUnexpectedEOF
					return
				}
				var parsed struct {
					Slots []struct {
						SlotID string `json:"slot_id"`
					} `json:"slots"`
				}
				if err := json.Unmarshal(rec.Body.Bytes(), &parsed); err != nil {
					errCh <- err
					return
				}
				if len(parsed.Slots) != 2 || parsed.Slots[0].SlotID != "slot-a" || parsed.Slots[1].SlotID != "slot-b" {
					errCh <- errorsNew("non-deterministic slot ordering in /trusted/status")
					return
				}
			}
		}()
	}

	wg.Wait()
	close(errCh)
	for err := range errCh {
		t.Fatalf("concurrent status/override test error: %v", err)
	}
}

type simpleError string

func (e simpleError) Error() string { return string(e) }
func errorsNew(msg string) error    { return simpleError(msg) }

func TestSSEAndWebSocketStreamKeepAlive(t *testing.T) {
	eng := newTestHubEngine(t)
	registerSlotForServerTest(t, eng, "slot-ka")
	authRep, gErr := eng.HandleSessionAuthorize(model.WireVersion, "core-ka", model.AuthorizeRequest{
		SlotID:         "slot-ka",
		RequestedScope: model.RequestedScopeSpec{AllowChat: true},
	})
	if gErr != nil {
		t.Fatalf("authorize: %v", gErr)
	}

	authCfg := server.AuthRegistry{
		OriginToken:             "orig-tok",
		CoreTokens:              map[string]string{"core-tok": "core-ka"},
		StreamKeepAliveInterval: 25 * time.Millisecond,
	}
	origSrv := httptest.NewServer(server.NewOriginHandler(eng, authCfg))
	defer origSrv.Close()
	coreSrv := httptest.NewServer(server.NewCoreHandler(eng, authCfg))
	defer coreSrv.Close()

	// 1. Verify Origin Execute SSE emits ready comment + keepalive comment while idle.
	{
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, origSrv.URL+"/g2a/v1/origin/execute-stream?slot_id=slot-ka&access_token=orig-tok", nil)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatalf("origin execute-stream GET: %v", err)
		}
		defer resp.Body.Close()

		reader := bufio.NewReader(resp.Body)
		sawReady := false
		sawKeepAlive := false
		for !(sawReady && sawKeepAlive) {
			line, err := reader.ReadString('\n')
			if err != nil {
				t.Fatalf("reading origin SSE: %v", err)
			}
			if strings.HasPrefix(line, ": g2a-origin-execute-stream-ready") {
				sawReady = true
			}
			if strings.HasPrefix(line, ": g2a-keepalive") {
				sawKeepAlive = true
			}
		}
	}

	// 2. Verify Core Events SSE emits ready comment + keepalive comment while idle.
	{
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, coreSrv.URL+"/g2a/v1/core/events?session_id="+authRep.SessionID+"&access_token=core-tok", nil)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatalf("core events GET: %v", err)
		}
		defer resp.Body.Close()

		reader := bufio.NewReader(resp.Body)
		sawReady := false
		sawKeepAlive := false
		for !(sawReady && sawKeepAlive) {
			line, err := reader.ReadString('\n')
			if err != nil {
				t.Fatalf("reading core SSE: %v", err)
			}
			if strings.HasPrefix(line, ": g2a-stream-ready") {
				sawReady = true
			}
			if strings.HasPrefix(line, ": g2a-keepalive") {
				sawKeepAlive = true
			}
		}
	}

	// 3. Verify Core WebSocket (/g2a/v1/core/ws) emits unsolicited RFC 6455 Ping (0x9), accepts masked Pong (0xA), and delivers live Text events (0x1).
	{
		hostPort := strings.TrimPrefix(coreSrv.URL, "http://")
		conn, err := net.DialTimeout("tcp", hostPort, 2*time.Second)
		if err != nil {
			t.Fatalf("dial core ws: %v", err)
		}
		defer conn.Close()
		_ = conn.SetDeadline(time.Now().Add(2 * time.Second))

		secKey := base64.StdEncoding.EncodeToString([]byte("0123456789abcdef"))
		handshake := "GET /g2a/v1/core/ws?session_id=" + authRep.SessionID + " HTTP/1.1\r\n" +
			"Host: " + hostPort + "\r\n" +
			"Authorization: Bearer core-tok\r\n" +
			"Upgrade: websocket\r\n" +
			"Connection: Upgrade\r\n" +
			"Sec-WebSocket-Version: 13\r\n" +
			"Sec-WebSocket-Key: " + secKey + "\r\n\r\n"
		if _, err := conn.Write([]byte(handshake)); err != nil {
			t.Fatalf("write ws handshake: %v", err)
		}

		br := bufio.NewReader(conn)
		statusLine, err := br.ReadString('\n')
		if err != nil || !strings.Contains(statusLine, "101") {
			t.Fatalf("expected HTTP 101 Switching Protocols, got %q (err=%v)", statusLine, err)
		}
		for {
			hdrLine, err := br.ReadString('\n')
			if err != nil {
				t.Fatalf("reading ws headers: %v", err)
			}
			if hdrLine == "\r\n" {
				break
			}
		}

		// Read unsolicited server Ping frame (opcode 0x9, payload "g2a").
		opcode, payload := readServerWSFrame(t, br)
		if opcode != 0x9 || string(payload) != "g2a" {
			t.Fatalf("expected WS Ping frame (0x9, 'g2a'), got opcode=0x%x payload=%q", opcode, string(payload))
		}

		// Reply with masked client Pong frame (FIN=1, opcode=0xA, MASK=1, len=3, mask=[1,2,3,4]).
		mask := [4]byte{1, 2, 3, 4}
		pongFrame := []byte{0x8A, 0x80 | 3, mask[0], mask[1], mask[2], mask[3], 'g' ^ mask[0], '2' ^ mask[1], 'a' ^ mask[2]}
		if _, err := conn.Write(pongFrame); err != nil {
			t.Fatalf("write masked pong: %v", err)
		}

		// Trigger a live player command and assert a Text frame (opcode 0x1) arrives over the same WebSocket.
		if gErr := eng.HandleOriginPlayerEvent(model.OriginPlayerEvent{
			SlotID: "slot-ka",
			Kind:   "player_command",
			Text:   "status check",
		}); gErr != nil {
			t.Fatalf("HandleOriginPlayerEvent: %v", gErr)
		}

		sawTextEvent := false
		for i := 0; i < 6 && !sawTextEvent; i++ {
			op, data := readServerWSFrame(t, br)
			if op == 0x1 && strings.Contains(string(data), `"context.push"`) && strings.Contains(string(data), `"status check"`) {
				sawTextEvent = true
			}
		}
		if !sawTextEvent {
			t.Fatal("did not receive live context.push Text frame over WebSocket after Ping/Pong keepalive")
		}
	}
}

func readServerWSFrame(t *testing.T, br *bufio.Reader) (byte, []byte) {
	t.Helper()
	b0, err := br.ReadByte()
	if err != nil {
		t.Fatalf("read ws b0: %v", err)
	}
	b1, err := br.ReadByte()
	if err != nil {
		t.Fatalf("read ws b1: %v", err)
	}
	if (b1 & 0x80) != 0 {
		t.Fatalf("server WS frame must NOT be masked")
	}
	n := int(b1 & 0x7F)
	if n == 126 {
		var ext [2]byte
		if _, err := io.ReadFull(br, ext[:]); err != nil {
			t.Fatalf("read ws ext16: %v", err)
		}
		n = int(ext[0])<<8 | int(ext[1])
	}
	payload := make([]byte, n)
	if n > 0 {
		if _, err := io.ReadFull(br, payload); err != nil {
			t.Fatalf("read ws payload: %v", err)
		}
	}
	return b0 & 0x0F, payload
}

func TestTrustedDashboardSSEPushAndSecurityHeaders(t *testing.T) {
	eng := newTestHubEngine(t)
	registerSlotForServerTest(t, eng, "slot-dash")

	dashSrv := httptest.NewServer(server.NewTrustedConsentHandler(eng, server.AuthRegistry{
		ConsentToken:            "dash-secret",
		StreamKeepAliveInterval: 50 * time.Millisecond,
	}))
	defer dashSrv.Close()

	// 1. Unauthenticated /trusted/events must be rejected with 401
	unauthResp, err := http.Get(dashSrv.URL + "/trusted/events")
	if err != nil {
		t.Fatalf("GET unauth /trusted/events: %v", err)
	}
	_ = unauthResp.Body.Close()
	if unauthResp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected 401 for unauthenticated /trusted/events, got %d", unauthResp.StatusCode)
	}

	// 2. Authenticated /trusted/events streams instant status.changed on policy or prompt updates
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, dashSrv.URL+"/trusted/events?access_token=dash-secret", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("GET /trusted/events: %v", err)
	}
	defer resp.Body.Close()

	if resp.Header.Get("X-Content-Type-Options") != "nosniff" {
		t.Fatalf("expected X-Content-Type-Options: nosniff, got %q", resp.Header.Get("X-Content-Type-Options"))
	}
	if resp.Header.Get("Referrer-Policy") != "no-referrer" {
		t.Fatalf("expected Referrer-Policy: no-referrer, got %q", resp.Header.Get("Referrer-Policy"))
	}

	reader := bufio.NewReader(resp.Body)
	readyLine, err := reader.ReadString('\n')
	if err != nil || !strings.HasPrefix(readyLine, ": g2a-dashboard-stream-ready") {
		t.Fatalf("expected dashboard stream ready comment, got %q (err=%v)", readyLine, err)
	}

	// Trigger a live policy override and assert event: status.changed arrives promptly
	if gErr := eng.SetPolicyOverride("slot-dash", "#01", "companion", "confirm"); gErr != nil {
		t.Fatalf("SetPolicyOverride: %v", gErr)
	}

	sawStatusChanged := false
	for i := 0; i < 12 && !sawStatusChanged; i++ {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatalf("reading /trusted/events: %v", err)
		}
		if strings.HasPrefix(line, "event: status.changed") {
			sawStatusChanged = true
		}
	}
	if !sawStatusChanged {
		t.Fatal("did not receive event: status.changed over /trusted/events")
	}
}
