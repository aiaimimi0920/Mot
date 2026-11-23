package server

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/hub"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/jcs"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/model"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/ui"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/ws"
)

type AuthRegistry struct {
	OriginToken  string
	CoreTokens   map[string]string // bearer token -> verified core_id
	ConsentToken string
	OriginAddr   string
	CoreAddr     string
}

func NewOriginHandler(engine *hub.Engine, auth AuthRegistry) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/g2a/v1/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", "g2a_version": model.WireVersion, "interface": "origin"})
	})
	mux.HandleFunc("/g2a/v1/origin", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeReplyError(w, "", nil, model.NewError(model.ErrInvalidRequest, 400, "POST required"))
			return
		}
		if auth.OriginToken != "" && extractBearer(r) != auth.OriginToken {
			writeReplyError(w, "", nil, model.NewError(model.ErrUnauthenticated, 401, "Invalid Origin bearer token"))
			return
		}
		env, gErr := decodeStrictRequestEnvelope(r)
		if gErr != nil {
			writeReplyError(w, "", nil, gErr)
			return
		}
		if env.G2AVersion != model.WireVersion {
			writeReplyError(w, env.RequestID, env.SessionID, model.NewError(model.ErrUnsupportedVersion, 400, "Require g2a_version 0.0.1"))
			return
		}

		switch env.Operation {
		case "origin.capabilities":
			var p model.OriginCapabilitiesPayload
			if err := json.Unmarshal(env.Payload, &p); err != nil {
				writeReplyError(w, env.RequestID, env.SessionID, model.NewError(model.ErrInvalidArguments, 400, err.Error()))
				return
			}
			if gErr := engine.HandleOriginCapabilities(p); gErr != nil {
				writeReplyError(w, env.RequestID, env.SessionID, gErr)
				return
			}
			writeReplyAccepted(w, env.RequestID, env.SessionID, map[string]any{"ok": true})

		case "origin.sync":
			var f model.OriginSyncFrame
			if err := json.Unmarshal(env.Payload, &f); err != nil {
				writeReplyError(w, env.RequestID, env.SessionID, model.NewError(model.ErrInvalidArguments, 400, err.Error()))
				return
			}
			if gErr := engine.HandleOriginSync(f); gErr != nil {
				writeReplyError(w, env.RequestID, env.SessionID, gErr)
				return
			}
			writeReplyAccepted(w, env.RequestID, env.SessionID, map[string]any{"rev": f.Rev})

		case "origin.player_event":
			var ev model.OriginPlayerEvent
			if err := json.Unmarshal(env.Payload, &ev); err != nil {
				writeReplyError(w, env.RequestID, env.SessionID, model.NewError(model.ErrInvalidArguments, 400, err.Error()))
				return
			}
			if gErr := engine.HandleOriginPlayerEvent(ev); gErr != nil {
				writeReplyError(w, env.RequestID, env.SessionID, gErr)
				return
			}
			writeReplyAccepted(w, env.RequestID, env.SessionID, map[string]any{"ok": true})

		default:
			writeReplyError(w, env.RequestID, env.SessionID, model.NewError(model.ErrInvalidRequest, 400, "Unknown Origin operation: "+env.Operation))
		}
	})

	// Zero-inbound-port Outbound Origin Execute Stream (GET /g2a/v1/origin/execute-stream?slot_id=...)
	mux.HandleFunc("/g2a/v1/origin/execute-stream", func(w http.ResponseWriter, r *http.Request) {
		if auth.OriginToken != "" && extractStreamToken(r) != auth.OriginToken {
			writeReplyError(w, "", nil, model.NewError(model.ErrUnauthenticated, 401, "Invalid Origin bearer token"))
			return
		}
		slotID := r.URL.Query().Get("slot_id")
		if slotID == "" {
			writeReplyError(w, "", nil, model.NewError(model.ErrInvalidRequest, 400, "slot_id query parameter required"))
			return
		}
		flusher, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "Streaming unsupported", http.StatusInternalServerError)
			return
		}
		ch, unsub := engine.OutboundOrigin().SubscribeSlot(slotID)
		defer unsub()

		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.Header().Set("Connection", "keep-alive")
		w.WriteHeader(http.StatusOK)
		_, _ = fmt.Fprint(w, ": g2a-origin-execute-stream-ready\n\n")
		flusher.Flush()

		ctx := r.Context()
		for {
			select {
			case <-ctx.Done():
				return
			case reqEnv := <-ch:
				raw, _ := json.Marshal(reqEnv)
				_, _ = fmt.Fprintf(w, "id: %s\nevent: origin.execute\ndata: %s\n\n", reqEnv.RequestID, string(raw))
				flusher.Flush()
			}
		}
	})

	// Reply endpoint for streamed "origin.execute" tickets (POST /g2a/v1/origin/execute-reply)
	mux.HandleFunc("/g2a/v1/origin/execute-reply", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeReplyError(w, "", nil, model.NewError(model.ErrInvalidRequest, 400, "POST required"))
			return
		}
		if auth.OriginToken != "" && extractBearer(r) != auth.OriginToken {
			writeReplyError(w, "", nil, model.NewError(model.ErrUnauthenticated, 401, "Invalid Origin bearer token"))
			return
		}
		raw, err := io.ReadAll(io.LimitReader(r.Body, 4<<20))
		if err != nil {
			writeReplyError(w, "", nil, model.NewError(model.ErrInvalidRequest, 400, "Failed reading reply body"))
			return
		}
		if _, errStrict := jcs.ParseStrictJSON(raw); errStrict != nil {
			writeReplyError(w, "", nil, model.NewError(model.ErrInvalidRequest, 400, errStrict.Error()))
			return
		}
		var replyEnv model.ReplyEnvelope
		if err := json.Unmarshal(raw, &replyEnv); err != nil || replyEnv.RequestID == "" {
			writeReplyError(w, "", nil, model.NewError(model.ErrInvalidRequest, 400, "Invalid ReplyEnvelope"))
			return
		}
		delivered := engine.OutboundOrigin().DeliverReply(replyEnv)
		writeReplyAccepted(w, replyEnv.RequestID, nil, map[string]any{"delivered": delivered})
	})

	return withAPICORS(mux)
}

func NewCoreHandler(engine *hub.Engine, auth AuthRegistry) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/g2a/v1/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", "g2a_version": model.WireVersion, "interface": "core"})
	})

	mux.HandleFunc("/g2a/v1/core", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeReplyError(w, "", nil, model.NewError(model.ErrInvalidRequest, 400, "POST required"))
			return
		}
		coreID, gErr := authenticateCore(r, auth)
		if gErr != nil {
			writeReplyError(w, "", nil, gErr)
			return
		}
		env, gErr := decodeStrictRequestEnvelope(r)
		if gErr != nil {
			writeReplyError(w, "", nil, gErr)
			return
		}
		if env.Operation != "session.authorize" && env.G2AVersion != model.WireVersion {
			writeReplyError(w, env.RequestID, env.SessionID, model.NewError(model.ErrUnsupportedVersion, 400, "Require g2a_version 0.0.1"))
			return
		}

		if env.Operation == "session.authorize" {
			var req model.AuthorizeRequest
			if err := json.Unmarshal(env.Payload, &req); err != nil {
				writeReplyError(w, env.RequestID, nil, model.NewError(model.ErrInvalidArguments, 400, err.Error()))
				return
			}
			for k, v := range env.Extensions {
				reqMap, _ := v.(map[string]any)
				reqFlag, _ := reqMap["required"].(bool)
				req.Extensions = append(req.Extensions, model.ExtensionSpec{Name: k, Required: reqFlag})
			}
			rep, errAuth := engine.HandleSessionAuthorize(env.G2AVersion, coreID, req)
			if errAuth != nil {
				writeReplyError(w, env.RequestID, nil, errAuth)
				return
			}
			sid := rep.SessionID
			writeReplyAccepted(w, env.RequestID, &sid, rep)
			return
		}

		if env.SessionID == nil || *env.SessionID == "" {
			writeReplyError(w, env.RequestID, nil, model.NewError(model.ErrInvalidRequest, 400, "session_id is required"))
			return
		}
		sid := *env.SessionID
		if rlErr := engine.CheckSessionRateLimit(sid); rlErr != nil {
			writeReplyError(w, env.RequestID, &sid, rlErr)
			return
		}

		switch env.Operation {
		case "session.heartbeat":
			rep, errOp := engine.HandleSessionHeartbeat(coreID, sid)
			if errOp != nil {
				writeReplyError(w, env.RequestID, &sid, errOp)
				return
			}
			writeReplyAccepted(w, env.RequestID, &sid, rep)

		case "session.resume":
			rep, errOp := engine.HandleSessionResume(coreID, sid)
			if errOp != nil {
				writeReplyError(w, env.RequestID, &sid, errOp)
				return
			}
			writeReplyAccepted(w, env.RequestID, &sid, rep)

		case "session.close":
			if errOp := engine.HandleSessionClose(coreID, sid); errOp != nil {
				writeReplyError(w, env.RequestID, &sid, errOp)
				return
			}
			writeReplyAccepted(w, env.RequestID, &sid, map[string]any{"closed": true})

		case "context.pull":
			rep, errOp := engine.HandleContextPull(coreID, sid)
			if errOp != nil {
				writeReplyError(w, env.RequestID, &sid, errOp)
				return
			}
			writeReplyAccepted(w, env.RequestID, &sid, rep)

		case "queue.submit":
			var q model.MethodCallQueue
			if err := json.Unmarshal(env.Payload, &q); err != nil {
				writeReplyError(w, env.RequestID, &sid, model.NewError(model.ErrInvalidArguments, 400, err.Error()))
				return
			}
			rep, errOp := engine.HandleQueueSubmit(coreID, sid, q)
			if errOp != nil {
				writeReplyError(w, env.RequestID, &sid, errOp)
				return
			}
			writeReplyAccepted(w, env.RequestID, &sid, rep)

		case "queue.cancel":
			var p model.QueueIDPayload
			if err := json.Unmarshal(env.Payload, &p); err != nil {
				writeReplyError(w, env.RequestID, &sid, model.NewError(model.ErrInvalidArguments, 400, err.Error()))
				return
			}
			rep, errOp := engine.HandleQueueCancel(coreID, sid, p.QueueID)
			if errOp != nil {
				writeReplyError(w, env.RequestID, &sid, errOp)
				return
			}
			writeReplyAccepted(w, env.RequestID, &sid, rep)

		case "queue.query":
			var p model.QueueIDPayload
			if err := json.Unmarshal(env.Payload, &p); err != nil {
				writeReplyError(w, env.RequestID, &sid, model.NewError(model.ErrInvalidArguments, 400, err.Error()))
				return
			}
			rep, errOp := engine.HandleQueueQuery(coreID, sid, p.QueueID)
			if errOp != nil {
				writeReplyError(w, env.RequestID, &sid, errOp)
				return
			}
			writeReplyAccepted(w, env.RequestID, &sid, rep)

		case "chat.send":
			var m model.ChatMessage
			if err := json.Unmarshal(env.Payload, &m); err != nil {
				writeReplyError(w, env.RequestID, &sid, model.NewError(model.ErrInvalidArguments, 400, err.Error()))
				return
			}
			if errOp := engine.HandleChatSend(coreID, sid, m); errOp != nil {
				writeReplyError(w, env.RequestID, &sid, errOp)
				return
			}
			writeReplyAccepted(w, env.RequestID, &sid, map[string]any{"delivered": true})

		default:
			writeReplyError(w, env.RequestID, &sid, model.NewError(model.ErrInvalidRequest, 400, "Unknown Core operation: "+env.Operation))
		}
	})

	mux.HandleFunc("/g2a/v1/core/events", func(w http.ResponseWriter, r *http.Request) {
		coreID, gErr := authenticateCoreStream(r, auth)
		if gErr != nil {
			writeReplyError(w, "", nil, gErr)
			return
		}
		sid := r.URL.Query().Get("session_id")
		if sid == "" {
			writeReplyError(w, "", nil, model.NewError(model.ErrInvalidRequest, 400, "session_id query parameter required"))
			return
		}
		if _, errHb := engine.HandleSessionHeartbeat(coreID, sid); errHb != nil {
			writeReplyError(w, "", &sid, errHb)
			return
		}
		flusher, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "Streaming unsupported", http.StatusInternalServerError)
			return
		}
		replay := r.URL.Query().Get("replay") == "1"
		ch, unsubscribe := engine.PushStream().Subscribe(sid, replay)
		defer unsubscribe()

		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.Header().Set("Connection", "keep-alive")
		w.WriteHeader(http.StatusOK)
		_, _ = fmt.Fprint(w, ": g2a-stream-ready\n\n")
		flusher.Flush()

		ctx := r.Context()
		for {
			select {
			case <-ctx.Done():
				return
			case ev := <-ch:
				raw, _ := json.Marshal(ev)
				_, _ = fmt.Fprintf(w, "id: %s\nevent: %s\ndata: %s\n\n", ev.EventID, ev.Event, string(raw))
				flusher.Flush()
			}
		}
	})

	// RFC 6455 WebSocket Push Stream (/g2a/v1/core/ws?session_id=...) per Section 6.2
	mux.HandleFunc("/g2a/v1/core/ws", func(w http.ResponseWriter, r *http.Request) {
		coreID, gErr := authenticateCoreStream(r, auth)
		if gErr != nil {
			writeReplyError(w, "", nil, gErr)
			return
		}
		sid := r.URL.Query().Get("session_id")
		if sid == "" {
			writeReplyError(w, "", nil, model.NewError(model.ErrInvalidRequest, 400, "session_id query parameter required"))
			return
		}
		if _, errHb := engine.HandleSessionHeartbeat(coreID, sid); errHb != nil {
			writeReplyError(w, "", &sid, errHb)
			return
		}
		replay := r.URL.Query().Get("replay") == "1"
		ch, unsubscribe := engine.PushStream().Subscribe(sid, replay)
		defer unsubscribe()

		wsConn, err := ws.Upgrade(w, r)
		if err != nil {
			return
		}
		defer wsConn.Close()

		closedCh := make(chan struct{})
		go func() {
			wsConn.ReadLoop()
			close(closedCh)
		}()

		for {
			select {
			case <-closedCh:
				return
			case ev := <-ch:
				raw, _ := json.Marshal(ev)
				if err := wsConn.WriteText(raw); err != nil {
					return
				}
			}
		}
	})

	return withAPICORS(mux)
}

func withAPICORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if origin != "" {
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Set("Vary", "Origin")
		} else {
			w.Header().Set("Access-Control-Allow-Origin", "*")
		}
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		w.Header().Set("Access-Control-Max-Age", "86400")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// NewTrustedConsentHandler serves the embedded Player Security Web Dashboard and local consent APIs.
func NewTrustedConsentHandler(engine *hub.Engine, auth AuthRegistry) http.Handler {
	var csrfBytes [16]byte
	_, _ = rand.Read(csrfBytes[:])
	csrfToken := hex.EncodeToString(csrfBytes[:])

	mux := http.NewServeMux()

	setSecurityHeaders := func(w http.ResponseWriter) {
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Content-Security-Policy", "frame-ancestors 'none'")
	}

	checkTrustedAuth := func(r *http.Request) bool {
		if r.Header.Get("X-G2A-CSRF") == csrfToken {
			return true
		}
		if auth.ConsentToken == "" {
			return true
		}
		return extractBearer(r) == auth.ConsentToken
	}

	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		if auth.ConsentToken != "" && extractBearer(r) != auth.ConsentToken && r.URL.Query().Get("token") != auth.ConsentToken {
			http.Error(w, "Unauthorized: supply local consent token", http.StatusUnauthorized)
			return
		}
		setSecurityHeaders(w)
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = io.WriteString(w, ui.RenderDashboardHTML(csrfToken, auth.CoreAddr, auth.OriginAddr))
	})

	mux.HandleFunc("/trusted/status", func(w http.ResponseWriter, r *http.Request) {
		if !checkTrustedAuth(r) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		setSecurityHeaders(w)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(engine.SnapshotDashboardStatus())
	})

	mux.HandleFunc("/trusted/pending", func(w http.ResponseWriter, r *http.Request) {
		if !checkTrustedAuth(r) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		setSecurityHeaders(w)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(engine.Consent().SnapshotPending())
	})

	mux.HandleFunc("/trusted/decide-scope", func(w http.ResponseWriter, r *http.Request) {
		if !checkTrustedAuth(r) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		var body struct {
			PromptID         string `json:"prompt_id"`
			Approved         bool   `json:"approved"`
			RememberAutoJoin bool   `json:"remember_auto_join"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		_, ok := engine.Consent().DecideScope(body.PromptID, body.Approved, body.RememberAutoJoin)
		setSecurityHeaders(w)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": ok})
	})

	mux.HandleFunc("/trusted/decide-queue", func(w http.ResponseWriter, r *http.Request) {
		if !checkTrustedAuth(r) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		var body struct {
			SessionID   string `json:"session_id"`
			QueueID     string `json:"queue_id"`
			QueueDigest string `json:"queue_digest"`
			Fingerprint string `json:"fingerprint"`
			Approved    bool   `json:"approved"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		engine.OnTrustedConfirmationResult(body.SessionID, body.QueueID, body.QueueDigest, body.Fingerprint, body.Approved)
		setSecurityHeaders(w)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	})

	mux.HandleFunc("/trusted/decide-resume", func(w http.ResponseWriter, r *http.Request) {
		if !checkTrustedAuth(r) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		var body struct {
			SessionID string `json:"session_id"`
			Approved  bool   `json:"approved"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		engine.OnTrustedResumeDecision(body.SessionID, body.Approved)
		setSecurityHeaders(w)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	})

	mux.HandleFunc("/trusted/policy-override", func(w http.ResponseWriter, r *http.Request) {
		if !checkTrustedAuth(r) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		var body struct {
			SlotID   string `json:"slot_id"`
			MethodID string `json:"method_id"`
			Actor    string `json:"actor"`
			Policy   string `json:"policy"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		setSecurityHeaders(w)
		if gErr := engine.SetPolicyOverride(body.SlotID, body.MethodID, body.Actor, body.Policy); gErr != nil {
			writeReplyError(w, "", nil, gErr)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	})

	mux.HandleFunc("/trusted/revoke-session", func(w http.ResponseWriter, r *http.Request) {
		if !checkTrustedAuth(r) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		var body struct {
			SessionID string `json:"session_id"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		setSecurityHeaders(w)
		if gErr := engine.RevokeSessionFromTrustedUI(body.SessionID); gErr != nil {
			writeReplyError(w, "", nil, gErr)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	})

	mux.HandleFunc("/trusted/emergency-stop", func(w http.ResponseWriter, r *http.Request) {
		if !checkTrustedAuth(r) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		var body struct {
			SlotID string `json:"slot_id"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		setSecurityHeaders(w)
		_ = engine.HandleOriginPlayerEvent(model.OriginPlayerEvent{
			SlotID: body.SlotID,
			Kind:   "emergency_stop",
		})
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	})

	mux.HandleFunc("/trusted/toggle-modality", func(w http.ResponseWriter, r *http.Request) {
		if !checkTrustedAuth(r) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		var body struct {
			SessionID   string `json:"session_id"`
			AllowVisual bool   `json:"allow_visual"`
			AllowAudio  bool   `json:"allow_audio"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		setSecurityHeaders(w)
		if gErr := engine.ToggleSessionModality(body.SessionID, body.AllowVisual, body.AllowAudio); gErr != nil {
			writeReplyError(w, "", nil, gErr)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	})

	mux.HandleFunc("/trusted/revoke-auto-scope", func(w http.ResponseWriter, r *http.Request) {
		if !checkTrustedAuth(r) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		var body struct {
			ScopeDigest           string `json:"scope_digest"`
			CloseMatchingSessions bool   `json:"close_matching_sessions"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		setSecurityHeaders(w)
		engine.RevokeAutoJoinScope(body.ScopeDigest, body.CloseMatchingSessions)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	})

	return mux
}

func authenticateCore(r *http.Request, auth AuthRegistry) (string, *model.G2AError) {
	return authenticateCoreWithToken(extractBearer(r), auth)
}

func authenticateCoreStream(r *http.Request, auth AuthRegistry) (string, *model.G2AError) {
	return authenticateCoreWithToken(extractStreamToken(r), auth)
}

func authenticateCoreWithToken(tok string, auth AuthRegistry) (string, *model.G2AError) {
	if tok == "" {
		return "", model.NewError(model.ErrUnauthenticated, 401, "Missing Bearer token")
	}
	if len(auth.CoreTokens) == 0 {
		return tok, nil
	}
	coreID, ok := auth.CoreTokens[tok]
	if !ok {
		return "", model.NewError(model.ErrUnauthenticated, 401, "Unrecognized Core token")
	}
	return coreID, nil
}

func extractBearer(r *http.Request) string {
	h := r.Header.Get("Authorization")
	if strings.HasPrefix(h, "Bearer ") {
		return strings.TrimSpace(strings.TrimPrefix(h, "Bearer "))
	}
	return ""
}

func extractStreamToken(r *http.Request) string {
	if tok := extractBearer(r); tok != "" {
		return tok
	}
	return strings.TrimSpace(r.URL.Query().Get("access_token"))
}

func decodeStrictRequestEnvelope(r *http.Request) (model.RequestEnvelope, *model.G2AError) {
	var env model.RequestEnvelope
	raw, err := io.ReadAll(io.LimitReader(r.Body, 4<<20))
	if err != nil {
		return env, model.NewError(model.ErrInvalidRequest, 400, "Failed reading request body")
	}
	if _, errStrict := jcs.ParseStrictJSON(raw); errStrict != nil {
		return env, model.NewError(model.ErrInvalidRequest, 400, errStrict.Error())
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return env, model.NewError(model.ErrInvalidRequest, 400, "Invalid RequestEnvelope JSON")
	}
	if env.RequestID == "" || env.Operation == "" {
		return env, model.NewError(model.ErrInvalidRequest, 400, "request_id and operation are required")
	}
	return env, nil
}

func writeReplyAccepted(w http.ResponseWriter, reqID string, sessionID *string, result any) {
	reply := model.ReplyEnvelope{
		G2AVersion:  model.WireVersion,
		RequestID:   reqID,
		SessionID:   sessionID,
		TimestampMs: time.Now().UnixMilli(),
		Acceptance:  model.AcceptanceAccepted,
		Result:      result,
		Error:       nil,
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(reply)
}

func writeReplyError(w http.ResponseWriter, reqID string, sessionID *string, gErr *model.G2AError) {
	acc := model.AcceptanceNotAccepted
	if gErr.Code == model.ErrOutcomeUnknown || gErr.HTTPStatus == 504 {
		acc = model.AcceptanceUnknown
	}
	if gErr.RetryAfterMs != nil {
		secs := (*gErr.RetryAfterMs + 999) / 1000
		if secs < 1 {
			secs = 1
		}
		w.Header().Set("Retry-After", strconv.FormatInt(secs, 10))
	}
	reply := model.ReplyEnvelope{
		G2AVersion:  model.WireVersion,
		RequestID:   reqID,
		SessionID:   sessionID,
		TimestampMs: time.Now().UnixMilli(),
		Acceptance:  acc,
		Result:      nil,
		Error:       gErr,
	}
	w.Header().Set("Content-Type", "application/json")
	status := gErr.HTTPStatus
	if status == 0 {
		status = http.StatusBadRequest
	}
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(reply)
}
