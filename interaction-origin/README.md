# Interaction Origin (`V0.0.1`)

Standalone G2A `V0.0.1` **Interaction Origin** multi-engine SDK suite and genre reference hosts for game developers and engine integrators.

Implements Tier 1 of [G2A_SPECIFICATION.zh-CN.md](../G2A/docs/standard/G2A_SPECIFICATION.zh-CN.md) and Chapter 2 of [G2A_PSEUDOCODE_API.zh-CN.md](../G2A/docs/standard/G2A_PSEUDOCODE_API.zh-CN.md):

- [`python/`](python/): Python 3.11+ `OriginSDK` (loopback listener + zero-inbound-port `pull://origin.execute` outbound stream + multiplayer `room_id`/`faction_id` slot tagging + multimodal `visual_ref`/`audio_ref` & `allowed_timeline_tags`) + all **five canonical genre reference hosts** in `demo_host.py`:
  - `--mode spatial`: Embodied 3D RPG (sustained `move_to` + `interact` bypassing hotkey E mutex, hidden secret redaction);
  - `--mode strategy_card`: Turn-based card battler (`companion` coaching overlay vs `player` seat, hidden draw-pile order redaction);
  - `--mode workshop`: Avatar-less simulation/management ledger (`signal` + `snapshot()`, halted-line alerts);
  - `--mode deduction`: Entity-less detective mystery board (`companion` non-intrusive clue overlay vs `player` accusation seat, unrevealed culprit redaction);
  - `--mode tactical_arena`: Real-time squad tactics (`faction` fog-of-war redaction, sustained squad rally/ability dispatch).
- [`csharp/`](csharp/): .NET 8 / Unity-compatible C# `G2A.InteractionOrigin` SDK (`OriginSdk`, `JcsDigest`, `HttpHubTransportClient.FromLocalHubDiscovery()`, `OutboundExecuteStreamClient`, `LoopbackExecuteListener`), plus a drop-in **Unity Package Manager (UPM) package** in [`csharp/UnityPackage/com.g2a.interaction-origin/`](csharp/UnityPackage/com.g2a.interaction-origin/) (`G2AUnityOriginBridge`, `G2ASpatialInteractable`, `G2AUnityMainThreadDispatcher`).
- [`gdscript/`](gdscript/): Godot 4.x native addon (`addons/g2a_origin/`) including `G2AOriginSDK` (with `auto_discover_local_hub()`), `G2AJcs`, `G2ALoopbackServer` (`TCPServer`), `G2ASpatialAdapter3D` (`RayCast3D` / XR pointing ray + `"g2a_hidden_secret"` redaction), and a complete runnable Godot 4.x project in [`gdscript/demo_project/project.godot`](gdscript/demo_project/project.godot).
- [`cpp/`](cpp/): Zero-dependency **C++17 header-only SDK** (`include/g2a/origin_sdk.hpp`, `jcs.hpp`, `sha256.hpp`) with `discover_local_hub_origin_url()` and multiplayer `room_id`/`faction_id` support for Unreal Engine & custom C++ engines (`g++ -std=c++17 -O2 -Wall -Wextra -Werror cpp/tests/self_test.cpp`).
- [`typescript/`](typescript/): Zero-dependency **TypeScript / ES2022 Web SDK** (`src/index.ts`, `src/jcs.ts`) for HTML5 / Three.js / Phaser / Cocos / Tauri games using WebCrypto JCS SHA-256 and browser `EventSource` zero-inbound-port streaming (`pull://origin.execute`), plus a zero-build playable **HTML5 Spatial + Workshop Hybrid Web Game** in [`typescript/web_demo/index.html`](typescript/web_demo/index.html).
