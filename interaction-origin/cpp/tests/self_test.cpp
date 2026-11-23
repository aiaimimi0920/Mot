#include <cassert>
#include <iostream>
#include <vector>

#include "../include/g2a/origin_sdk.hpp"

int main() {
    using namespace g2a;

    // 1. Cross-language Golden RFC 8785 (JCS) SHA-256 vector parity check (matches Go, C#, and Python byte-for-byte!)
    JsonValue v1 = JsonObject{
        {"steps", JsonArray{
            JsonObject{
                {"args", JsonObject{{"qty", 20.0}, {"item", "timber"}}},
                {"step_index", 0}
            }
        }},
        {"queue_id", "Q-205"},
        {"session_id", "sess-1"}
    };
    std::string digest = jcs::digest_sha256("g2a.confirm.v1", v1);
    const std::string kGolden = "sha256:8d8634fd72ae48e655a9151e50b06a3e1095e753cacb2c293706e59af1b9ccd8";
    if (digest != kGolden) {
        std::cerr << "C++17 JCS digest mismatch: " << digest << " != " << kGolden << "\n";
        return 1;
    }

    // 2. Verify independent_npc strips "player" actor
    OriginSdk npc_sdk("npc-guard", [](const std::string&, const JsonValue&) {}, "independent_npc");
    npc_sdk.register_capability(
        {"#05", "patrol", "Patrol sector", {"companion", "player"}, {}, false, {}, "allow"},
        [](const std::string&, const JsonObject&, const std::string&) { return EffectOutcome::ImmediateSucceeded(); });
    assert(npc_sdk.get_capability("#05").permitted_actors.size() == 1);
    assert(npc_sdk.get_capability("#05").permitted_actors[0] == "companion");

    // 3. Verify Zero-Change Zero-Packet, Sustained Step Settlement, and Mid-Flight Cancellation
    std::vector<std::pair<std::string, JsonValue>> posts;
    bool at_chest = false;
    bool cancelled = false;

    OriginSdk sdk("slot-cpp", [&](const std::string& op, const JsonValue& payload) {
        posts.emplace_back(op, payload);
    });
    sdk.bind_actor_subject("companion");
    sdk.bind_state_module(
        "dungeon",
        [&]() { return JsonObject{{"at_chest", at_chest}}; },
        [&]() {
            return std::vector<ActiveMethodGrant>{
                {"#01", "companion", "ready", {{"chest_R", "Right Chest", "ready", std::nullopt, "4.0m", "right 4.0m", std::nullopt}}},
                {"#02", "companion", at_chest ? "ready" : "staged", {{"chest_R", "Right Chest", at_chest ? "ready" : "staged", std::nullopt, std::nullopt, std::nullopt, std::nullopt}}},
            };
        });

    sdk.register_capability(
        {"#01", "move_to", "Move", {"companion"}, {}, true, {}, "allow"},
        [&](const std::string&, const JsonObject&, const std::string&) {
            return EffectOutcome::StartedExecuting([&]() { cancelled = true; });
        });
    sdk.register_capability(
        {"#02", "interact", "Open", {"companion"}, {}, false, {}, "allow"},
        [&](const std::string&, const JsonObject&, const std::string&) {
            return EffectOutcome::ImmediateSucceeded(JsonObject{{"loot", "gold_50"}});
        });

    sdk.publish_capabilities_and_baseline();
    assert(posts.size() == 2);
    assert(!sdk.flush_dirty_sync(false)); // Zero-change zero-packet!
    assert(posts.size() == 2);

    auto r1 = sdk.handle_origin_execute({"slot-cpp", 1, "invoke", "exec-cpp-1", "#01", "companion", {}});
    assert(r1.state == "executing");
    at_chest = true;
    sdk.complete_step("exec-cpp-1", true, JsonObject{{"arrived", "chest_R"}});
    assert(posts.size() == 3); // Atomic delta frame carrying both settled exec-cpp-1 and #02 ready!

    auto r2 = sdk.handle_origin_execute({"slot-cpp", 1, "invoke", "exec-cpp-2", "#01", "companion", {}});
    assert(r2.state == "executing");
    auto rc = sdk.handle_origin_execute({"slot-cpp", 1, "cancel", "exec-cpp-2", std::nullopt, std::nullopt, {}});
    assert(cancelled && rc.state == "cancelled");

    std::cout << "C++17 G2A OriginSdk SelfTest PASSED | JCS=" << digest << "\n";
    return 0;
}
