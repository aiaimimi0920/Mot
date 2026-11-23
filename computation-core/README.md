# Computation Core (`V0.0.1`)

Standalone G2A `V0.0.1` **Computation Core** SDK, OpenAI/Ollama-compatible LLM cognitive planner with prompt-injection firewall, and SQLite portable companion memory manager in Python 3.11+ (zero mandatory third-party dependencies).

Implements Tier 3 of [G2A_SPECIFICATION.zh-CN.md](../G2A/docs/standard/G2A_SPECIFICATION.zh-CN.md) and Chapter 4 of [G2A_PSEUDOCODE_API.zh-CN.md](../G2A/docs/standard/G2A_PSEUDOCODE_API.zh-CN.md):

- **Outbound SSRF Guard & RFC 8785 (JCS) Digests:** `ssrf.py` & `jcs.py`.
- **`CoreSDK` Client State Machine (`core_sdk.py`):** `session.authorize` with `cached_capability_digest` + multiplayer `player_id` / `faction_id`, background `session.heartbeat`, **SSE (`stream_mode="sse"`) & RFC 6455 WebSocket (`stream_mode="websocket"`, including TLS 1.3 `wss://`)** push stream consumers, two-tier `session.resume` (`actions_paused` enforcement), `409 confirmation_required` `queue_id` preservation, `409 stale_context` `fresh_context` replanning, and `504 outcome_unknown` mandatory `queue.query` reconciliation.
- **TypeScript / Node / Deno / Browser `CoreSDK` (`typescript/` — `@g2a/computation-core`):** Zero-dependency ES2022 TypeScript SDK implementing WebCrypto JCS SHA-256 (`g2a.confirm.v1` / `g2a.capabilities.v1`), SSRF guard, SSE & WebSocket streams, `submitActionQueue` digest preservation, and `g2a.memory.bundle.v1` portability.
- **Cloud NAT Relay & Cross-Vendor Memory HTTP Server (`relay_server.py`, `memory_server.py`):** Accepts outbound Hub NAT tunnels for cloud-hosted Cores and exposes standard HTTP `g2a.memory.bundle.v1` export/import endpoints.
- **LLM Planner & Post-LLM Structural Firewall (`llm_planner.py`):** Connects to Ollama / OpenAI / vLLM / Gemini OpenAI-compatible `/v1/chat/completions`, isolates untrusted game text inside `<UNTRUSTED_GAME_DATA>`, and drops any hallucinated or injected step outside active grants before `queue.submit`.
- **SQLite Durable Companion Memory & Open Portability (`sqlite_memory.py`, `memory.py`):** ACID WAL SQLite store supporting player `list / add / edit / pin / unpin / delete / export / import`, `g2a.memory.bundle.v1` unrestricted export, conservative disclosure narrowing on import, and save-reload anti-spoiler guards.

## Test & CLI Usage

```sh
python -m unittest discover -s tests -v

# One-command 3-tier full-stack demo across all 5 canonical game genres via zero-config auto-discovery
PYTHONPATH=src python -m g2a_core.cli demo --genre all

# Verify all 14 normative G2A V0.0.1 error codes against a running Intelligent Hub
PYTHONPATH=src python -m g2a_core.cli conformance

# Connect via zero-config auto-discovery (--hub auto) with local Ollama / OpenAI LLM planner
PYTHONPATH=src python -m g2a_core.cli run --hub auto --slot slot-spatial --planner llm --llm-url http://127.0.0.1:11434/v1 --llm-model qwen2.5:7b

# Manage & export player-owned companion memories
PYTHONPATH=src python -m g2a_core.cli memory add --db companion.db --content "Prefers stealth approach" --pin
PYTHONPATH=src python -m g2a_core.cli memory export --db companion.db --file bundle.json
```
