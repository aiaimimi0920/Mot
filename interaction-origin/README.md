# Interaction Origin (`V0.0.1`)

Standalone G2A `V0.0.1` **Interaction Origin** multi-engine SDK suite and genre reference hosts for game developers and engine integrators.

Implements Tier 1 of [G2A_SPECIFICATION.zh-CN.md](../G2A/docs/standard/G2A_SPECIFICATION.zh-CN.md) and Chapter 2 of [G2A_PSEUDOCODE_API.zh-CN.md](../G2A/docs/standard/G2A_PSEUDOCODE_API.zh-CN.md):

- [`python/`](python/): Python 3.11+ `OriginSDK` + loopback `origin.execute` listener + three playable genre reference hosts in `demo_host.py`:
  - `--mode spatial`: Embodied 3D RPG (sustained `move_to` + `interact` bypassing hotkey E mutex, hidden secret redaction);
  - `--mode workshop`: Avatar-less simulation/management ledger (`signal` + `snapshot()`, halted-line alerts);
  - `--mode deduction`: Entity-less detective mystery board (`companion` non-intrusive clue overlay vs `player` accusation seat, unrevealed culprit redaction).
- [`csharp/`](csharp/): .NET 8 / Unity-compatible C# `G2A.InteractionOrigin` SDK including `OriginSdk`, `JcsDigest`, `HttpHubTransportClient`, and unprivileged `LoopbackExecuteListener` (`TcpListener`), plus live Hub host mode (`--live-hub`).
- [`gdscript/`](gdscript/): Godot 4.x native addon (`addons/g2a_origin/`) including `G2AOriginSDK` (with `auto_discover_local_hub()`), `G2AJcs`, `G2ALoopbackServer` (`TCPServer`), `G2ASpatialAdapter3D` (`RayCast3D` / XR pointing ray + `"g2a_hidden_secret"` redaction), and a complete runnable Godot 4.x project in [`gdscript/demo_project/project.godot`](gdscript/demo_project/project.godot).
