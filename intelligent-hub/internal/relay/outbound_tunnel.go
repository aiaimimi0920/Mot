package relay

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"time"

	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/hub"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/model"
)

type DownlinkRequestFrame struct {
	TunnelReqID string          `json:"tunnel_req_id"`
	CoreBearer  string          `json:"core_bearer"`
	Body        json.RawMessage `json:"body"`
}

type UplinkFrame struct {
	Kind        string               `json:"kind"` // "reply" | "event"
	TunnelReqID string               `json:"tunnel_req_id,omitempty"`
	HTTPStatus  int                  `json:"http_status,omitempty"`
	Reply       json.RawMessage      `json:"reply,omitempty"`
	Event       *model.EventEnvelope `json:"event,omitempty"`
}

// OutboundCloudTunnel implements G2A Section 2.2 Row 3 ("NAT Outbound Connection Mode"):
// A client Intelligent Hub behind a home firewall dials outbound to a Cloud Relay / Computation Core,
// forwarding Core Interface calls and SSE events without opening ANY inbound port on the player's network.
type OutboundCloudTunnel struct {
	relayBaseURL string
	hubID        string
	relayToken   string
	coreHandler  http.Handler
	engine       *hub.Engine
	httpClient   *http.Client
	streamClient *http.Client
}

func NewOutboundCloudTunnel(relayBaseURL, hubID, relayToken string, coreHandler http.Handler, engine *hub.Engine) *OutboundCloudTunnel {
	noProxyTransport := &http.Transport{Proxy: nil}
	return &OutboundCloudTunnel{
		relayBaseURL: strings.TrimRight(relayBaseURL, "/"),
		hubID:        hubID,
		relayToken:   relayToken,
		coreHandler:  coreHandler,
		engine:       engine,
		httpClient:   &http.Client{Timeout: 10 * time.Second, Transport: noProxyTransport},
		streamClient: &http.Client{Timeout: 0, Transport: noProxyTransport},
	}
}

func (t *OutboundCloudTunnel) Start(ctx context.Context) {
	go t.downlinkLoop(ctx)
}

func (t *OutboundCloudTunnel) downlinkLoop(ctx context.Context) {
	subscribedSessions := make(map[string]context.CancelFunc)
	defer func() {
		for _, cancel := range subscribedSessions {
			cancel()
		}
	}()

	url := fmt.Sprintf("%s/g2a/v1/relay/down?hub_id=%s", t.relayBaseURL, t.hubID)
	for {
		if ctx.Err() != nil {
			return
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return
		}
		req.Header.Set("Accept", "text/event-stream")
		if t.relayToken != "" {
			req.Header.Set("Authorization", "Bearer "+t.relayToken)
		}

		resp, err := t.streamClient.Do(req)
		if err != nil {
			select {
			case <-ctx.Done():
				return
			case <-time.After(250 * time.Millisecond):
				continue
			}
		}

		reader := bufio.NewReader(resp.Body)
		var dataLines []string
		for {
			line, readErr := reader.ReadString('\n')
			if readErr != nil {
				_ = resp.Body.Close()
				break
			}
			line = strings.TrimRight(line, "\r\n")
			if line == "" {
				if len(dataLines) > 0 {
					joined := strings.Join(dataLines, "\n")
					dataLines = dataLines[:0]
					t.handleDownlinkFrame(ctx, joined, subscribedSessions)
				}
			} else if strings.HasPrefix(line, "data:") {
				dataLines = append(dataLines, strings.TrimLeft(line[5:], " "))
			}
		}
	}
}

func (t *OutboundCloudTunnel) handleDownlinkFrame(
	ctx context.Context,
	rawJSON string,
	subscribedSessions map[string]context.CancelFunc,
) {
	var frame DownlinkRequestFrame
	if err := json.Unmarshal([]byte(rawJSON), &frame); err != nil || frame.TunnelReqID == "" {
		return
	}

	rec := httptest.NewRecorder()
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, "/g2a/v1/core", bytes.NewReader(frame.Body))
	if err != nil {
		return
	}
	httpReq.Header.Set("Content-Type", "application/json")
	if frame.CoreBearer != "" {
		httpReq.Header.Set("Authorization", "Bearer "+frame.CoreBearer)
	}
	t.coreHandler.ServeHTTP(rec, httpReq)

	respBytes := rec.Body.Bytes()
	// If a new session was authorized over the tunnel, automatically forward its push events uplink!
	var replyEnv model.ReplyEnvelope
	if err := json.Unmarshal(respBytes, &replyEnv); err == nil && replyEnv.SessionID != nil {
		sid := *replyEnv.SessionID
		if sid != "" {
			if _, exists := subscribedSessions[sid]; !exists {
				subCtx, cancel := context.WithCancel(ctx)
				subscribedSessions[sid] = cancel
				go t.forwardSessionEventsUplink(subCtx, sid)
			}
		}
	}

	_ = t.postUplink(ctx, UplinkFrame{
		Kind:        "reply",
		TunnelReqID: frame.TunnelReqID,
		HTTPStatus:  rec.Code,
		Reply:       json.RawMessage(respBytes),
	})
}

func (t *OutboundCloudTunnel) forwardSessionEventsUplink(ctx context.Context, sessionID string) {
	ch, unsub := t.engine.PushStream().Subscribe(sessionID, false)
	defer unsub()
	for {
		select {
		case <-ctx.Done():
			return
		case ev := <-ch:
			evCopy := ev
			_ = t.postUplink(ctx, UplinkFrame{
				Kind:  "event",
				Event: &evCopy,
			})
		}
	}
}

func (t *OutboundCloudTunnel) postUplink(ctx context.Context, up UplinkFrame) error {
	raw, err := json.Marshal(up)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, fmt.Sprintf("%s/g2a/v1/relay/up?hub_id=%s", t.relayBaseURL, t.hubID), bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if t.relayToken != "" {
		req.Header.Set("Authorization", "Bearer "+t.relayToken)
	}
	resp, err := t.httpClient.Do(req)
	if err != nil {
		return err
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	return nil
}
