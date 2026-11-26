# G2A C++17 Header-Only & Unreal Engine 5 Interaction Origin SDK (`0.0.1`)

- **Header-Only CMake / pkg-config Library (`G2A::Origin`):** `include/g2a/origin_sdk.hpp`, `http_transport.hpp`, `jcs.hpp`, `sha256.hpp`.
- **Unreal Engine 5 Plugin (`G2AInteractionOrigin`):** `UnrealPlugin/G2AInteractionOrigin/` (`UG2AOriginSubsystem`).

## CMake Integration

```cmake
find_package(G2AOrigin 0.0.1 REQUIRED)
target_link_libraries(my_game PRIVATE G2A::Origin)
```
