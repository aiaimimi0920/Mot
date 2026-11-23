# Intelligent Hub (`V0.0.1`)

Standalone G2A `V0.0.1` **Intelligent Hub** security shield, queue scheduler, and local player control center implemented in pure Go (zero external dependencies; compiles to a single standalone binary).

Implements Tier 2 of [G2A_SPECIFICATION.zh-CN.md](../G2A/docs/standard/G2A_SPECIFICATION.zh-CN.md) and Chapter 3 of [G2A_PSEUDOCODE_API.zh-CN.md](../G2A/docs/standard/G2A_PSEUDOCODE_API.zh-CN.md):

- **Three Physically Separated Listeners:**
  1. **Origin Interface (`--origin-addr`, default `127.0.0.1:19210`):** `origin.capabilities`, `origin.sync` (`full` / `delta` + atomic `settled_executions`), `origin.player_event` (`player_command`, `presence_changed`, `emergency_stop`, `queue_approval`), and outbound `origin.execute` (`invoke` / `cancel` / `deliver_chat`).
  2. **Core Interface (`--core-addr`, default `127.0.0.1:19220`, TLS 1.3 via `--tls-cert` / `--tls-key` or zero-config ephemeral ECDSA P-256 via `--tls-self-signed`):** `session.authorize`, `session.heartbeat`, `session.resume`, `session.close`, `context.pull`, `queue.submit`, `queue.cancel`, `queue.query`, `chat.send`, plus **SSE** (`/g2a/v1/core/events`) and **RFC 6455 WebSocket** (`/g2a/v1/core/ws`) real-time push streams (`context.push`, `queue.confirmed`, `queue.declined`, `session.resumed`, `session.closed`), and client-side NAT outbound relay tunneling (`--relay-url`).
  3. **Trusted Player Security & Control Web Dashboard (`--consent-addr`, default `http://127.0.0.1:19230/`):** Embedded anti-clickjacking (`X-Frame-Options: DENY`) & CSRF-protected browser UI for scope approvals, exact-`scope_digest` auto-join rules, digest-bound single-prompt queue confirmation, policy overrides (`real_money` non-downgradable), emergency stop, session revocation, dual-track Core directory, and audit log.
- **Multiplayer Room & Faction Confidentiality Isolation:** Enforces `403 permission_denied` against cross-faction slot attachment and cross-player `companion` observation; keeps `developer_sponsored` room NPCs running until the last online human in the `room_id` disconnects (`remaining_online_humans == 0`).
- **Durable Atomic Persistence & Lease Reaper:** Crash-resilient atomic ledger (`--ledger`) + background lease expiration reaper transitioning stale sessions to `disconnected_grace` (`actions_paused = true` on resume).

## Build, Test & Run

```sh
go test -v ./...
go build -o bin/intelligent-hub.exe ./cmd/intelligent-hub

# Starts Hub, writes OS runtime discovery manifest (%LOCALAPPDATA%/G2A/hub.runtime.json or ~/.g2a/hub.runtime.json), and opens Trusted Web Dashboard
./bin/intelligent-hub.exe -ledger .hub-data/ledger.json -open-dashboard
```
