# Computation Core (`V0.0.1`)

Standalone G2A `V0.0.1` **Computation Core** polyglot SDK suite, OpenAI/Ollama-compatible LLM cognitive planner with prompt-injection firewall, NAT outbound cloud relay, and SQLite/portable companion memory manager.

Implements Tier 3 of [G2A_SPECIFICATION.zh-CN.md](../G2A/docs/standard/G2A_SPECIFICATION.zh-CN.md) and Chapter 4 of [G2A_PSEUDOCODE_API.zh-CN.md](../G2A/docs/standard/G2A_PSEUDOCODE_API.zh-CN.md) across **4 languages** with zero mandatory third-party dependencies:

- **Python 3.11+ Core Runtime & SDK (`src/g2a_core/`):**
  - `CoreSDK` (`core_sdk.py`), SSRF guard (`ssrf.py`), C-accelerated RFC 8785 JCS (`jcs.py`), `pull_context_if_due()`, SSE & RFC 6455 WebSocket (`wss://` TLS 1.3) consumers with `Last-Event-ID` cursor replay, early-push confirmation race protection, `409` / `504` standard recovery, NAT `CloudRelayServer` (`relay_server.py`), `OpenAICompatibleLLMPlanner` (`llm_planner.py`), and ACID WAL `SQLiteCompanionStore` + `MemoryHTTPServer` (`sqlite_memory.py`, `memory_server.py`).
- **TypeScript / Node 20+ / Deno / Bun / Browser SDK (`typescript/` — `@g2a/computation-core`):**
  - Zero-build `esm/index.mjs` + TypeScript `src/index.ts` implementing WebCrypto JCS SHA-256, SSRF guard, Browser WebSocket + Node 20 native RFC 6455 WebSocket + universal `fetch` `ReadableStream` SSE consumer, `pullContextIfDue()`, `MemoryService` (`g2a.memory.bundle.v1`), and `OpenAICompatibleLLMPlanner` (`<UNTRUSTED_GAME_DATA>` firewall).
- **Go 1.22+ Core SDK (`go/` — `github.com/aiaimimi0920/g2a-computation-core`):**
  - High-concurrency zero-dependency Go `CoreSDK` (`core_sdk.go`), strict I-JSON + RFC 8785 UTF-16 JCS (`jcs.go`), `netip` SSRF guard (`ssrf.go`), SSE & RFC 6455 WebSocket consumers, `PullContextIfDue()`, `MemoryService` (`memory.go`), and `OpenAICompatibleLLMPlanner` (`llm_planner.go`).
- **C# (.NET 8) Core SDK (`csharp/G2A.ComputationCore/`):**
  - Zero-dependency .NET 8 `CoreSdk` (`ClientWebSocket` + SSE streaming with `Last-Event-ID` replay, `PullContextIfDueAsync()`, `SubmitQueueWithRecoveryAsync()`), `JcsDigest`, `SsrfGuard`, `MemoryService` (`g2a.memory.bundle.v1`), and `OpenAICompatibleLlmPlanner`.

## Test & CLI Usage

```sh
# Python E2E, 14-error conformance, NAT relay, & stress suite
python -m unittest discover -s tests -v

# Go, C#, & TypeScript Node 20 Core SDK self-tests & live 3-tier E2E suites
cd go && go test -v ./...
dotnet run --project csharp/G2A.ComputationCore.SelfTest
node typescript/tests/self_test.mjs

# Launch live 3-application visual browser playground
PYTHONPATH=src python -m g2a_core.cli playground
```
