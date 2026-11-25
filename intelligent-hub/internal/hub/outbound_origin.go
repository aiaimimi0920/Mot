package hub

import (
	"encoding/json"
	"strings"
	"sync"
	"time"

	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/model"
)

const OutboundPullExecuteURL = "pull://origin.execute"

// OutboundReplyEnvelope avoids double Marshal/Unmarshal on Result when decoding POST /g2a/v1/origin/execute-reply.
type OutboundReplyEnvelope struct {
	G2AVersion  string                `json:"g2a_version"`
	RequestID   string                `json:"request_id"`
	TimestampMs int64                 `json:"timestamp_ms"`
	Acceptance  model.AcceptanceState `json:"acceptance"`
	Result      json.RawMessage       `json:"result"`
	Error       *model.G2AError       `json:"error"`
}

// OriginOutboundStreamBroker enables zero-inbound-port games (NAT / console / strict sandbox)
// to receive Hub -> Origin "origin.execute" envelopes over an outbound stream and post ReplyEnvelopes back.
type OriginOutboundStreamBroker struct {
	mu          sync.Mutex
	slotStreams map[string][]chan model.RequestEnvelope
	waiters     map[string]chan OutboundReplyEnvelope // request_id -> reply channel
	timeout     time.Duration
}

func NewOriginOutboundStreamBroker(timeout time.Duration) *OriginOutboundStreamBroker {
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	return &OriginOutboundStreamBroker{
		slotStreams: make(map[string][]chan model.RequestEnvelope),
		waiters:     make(map[string]chan OutboundReplyEnvelope),
		timeout:     timeout,
	}
}

func (b *OriginOutboundStreamBroker) SubscribeSlot(slotID string) (chan model.RequestEnvelope, func()) {
	ch := make(chan model.RequestEnvelope, 32)
	b.mu.Lock()
	b.slotStreams[slotID] = append(b.slotStreams[slotID], ch)
	b.mu.Unlock()

	return ch, func() {
		b.mu.Lock()
		if list, ok := b.slotStreams[slotID]; ok {
			kept := list[:0]
			for _, existing := range list {
				if existing != ch {
					kept = append(kept, existing)
				}
			}
			if len(kept) == 0 {
				delete(b.slotStreams, slotID)
			} else {
				b.slotStreams[slotID] = kept
			}
		}
		b.mu.Unlock()
	}
}

func (b *OriginOutboundStreamBroker) DeliverReply(reply OutboundReplyEnvelope) bool {
	b.mu.Lock()
	ch, ok := b.waiters[reply.RequestID]
	if ok {
		delete(b.waiters, reply.RequestID)
	}
	b.mu.Unlock()
	if !ok {
		return false
	}
	ch <- reply
	return true
}

func (b *OriginOutboundStreamBroker) DispatchOverStream(req model.OriginExecuteRequest) (model.OriginExecuteReply, *model.G2AError) {
	var zero model.OriginExecuteReply
	payloadBytes, err := json.Marshal(req)
	if err != nil {
		return zero, model.NewError(model.ErrInvalidRequest, 400, err.Error())
	}
	reqID := generateID("pull-req")
	env := model.RequestEnvelope{
		G2AVersion:  model.WireVersion,
		RequestID:   reqID,
		TimestampMs: time.Now().UnixMilli(),
		Operation:   "origin.execute",
		Payload:     payloadBytes,
		Extensions:  map[string]any{},
	}

	replyCh := make(chan OutboundReplyEnvelope, 1)
	b.mu.Lock()
	streams := b.slotStreams[req.SlotID]
	if len(streams) == 0 {
		b.mu.Unlock()
		return zero, model.NewError(model.ErrUnavailable, 503, "Origin outbound execute stream is not connected")
	}
	b.waiters[reqID] = replyCh
	delivered := false
	// Prefer the most recently connected subscriber first so reconnected Origin clients are never shadowed by half-open sockets.
	for i := len(streams) - 1; i >= 0; i-- {
		select {
		case streams[i] <- env:
			delivered = true
		default:
		}
		if delivered {
			break
		}
	}
	if !delivered {
		delete(b.waiters, reqID)
		b.mu.Unlock()
		return zero, model.NewError(model.ErrUnavailable, 503, "Origin outbound execute stream buffer full")
	}
	b.mu.Unlock()

	select {
	case replyEnv := <-replyCh:
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
	case <-time.After(b.timeout):
		b.mu.Lock()
		delete(b.waiters, reqID)
		b.mu.Unlock()
		return zero, model.NewError(model.ErrOutcomeUnknown, 504, "Timed out awaiting Origin outbound execute reply")
	}
}

// HybridOriginClient routes Hub -> Origin "origin.execute" either via direct HTTP POST or via zero-port outbound stream.
type HybridOriginClient struct {
	httpClient *HTTPOriginClient
	broker     *OriginOutboundStreamBroker
}

func NewHybridOriginClient(sharedToken string, broker *OriginOutboundStreamBroker) *HybridOriginClient {
	return &HybridOriginClient{
		httpClient: NewHTTPOriginClient(sharedToken),
		broker:     broker,
	}
}

func (h *HybridOriginClient) Execute(targetURL string, req model.OriginExecuteRequest) (model.OriginExecuteReply, *model.G2AError) {
	if targetURL == "" || strings.HasPrefix(targetURL, "pull://") {
		return h.broker.DispatchOverStream(req)
	}
	return h.httpClient.Execute(targetURL, req)
}
