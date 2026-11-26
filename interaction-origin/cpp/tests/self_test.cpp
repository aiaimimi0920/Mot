#include <cassert>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <vector>

#include "../include/g2a/http_transport.hpp"
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

    // 4. Verify local Hub runtime manifest discovery & multiplayer room/faction capability tags
    const std::string tmp_manifest = "tmp_hub_runtime_test.json";
    {
        std::ofstream ofs(tmp_manifest);
        ofs << R"({"g2a_version":"0.0.1","origin_url":"http://127.0.0.1:29210","core_url":"http://127.0.0.1:29220"})";
    }
    std::string discovered_url = OriginSdk::discover_local_hub_origin_url(tmp_manifest);
    std::remove(tmp_manifest.c_str());
    assert(discovered_url == "http://127.0.0.1:29210");

    JsonObject last_cap_payload;
    OriginSdk mp_sdk(
        "slot-mp-red",
        [&](const std::string& op, const JsonValue& p) {
            if (op == "origin.capabilities") last_cap_payload = std::get<JsonObject>(p.data);
        },
        "companion",
        "player_metered",
        "room-arena-1",
        "faction-red");
    mp_sdk.publish_capabilities_and_baseline("pull://origin.execute");
    assert(std::get<std::string>(last_cap_payload.at("room_id").data) == "room-arena-1");
    assert(std::get<std::string>(last_cap_payload.at("faction_id").data) == "faction-red");

    // 5. Verify 35-entity crowd priority sorting, multi-module grant merge, emergency stop, & epoch advance
    std::vector<std::pair<std::string, JsonValue>> crowd_posts;
    bool crowd_step_cancelled = false;
    OriginSdk crowd_sdk("slot-crowd-cpp", [&](const std::string& op, const JsonValue& p) {
        crowd_posts.emplace_back(op, p);
    });
    crowd_sdk.bind_actor_subject("companion");
    crowd_sdk.register_capability(
        {"#02", "interact", "Interact", {"companion"}, {}, true, {}, "allow"},
        [&](const std::string&, const JsonObject&, const std::string&) {
            return EffectOutcome::StartedExecuting([&]() { crowd_step_cancelled = true; });
        });

    crowd_sdk.bind_state_module(
        "crowd_far",
        []() { return JsonObject{{"sector", "north"}}; },
        []() {
            std::vector<TargetCandidate> cands;
            for (int i = 0; i < 32; ++i) {
                char id_buf[32];
                std::snprintf(id_buf, sizeof(id_buf), "crate_%02d", i);
                cands.push_back({id_buf, "Far Crate", "staged", "requires_proximity", "20.0m", std::nullopt, std::nullopt});
            }
            cands.push_back({"station_alert", "Halted Loom", "staged", std::nullopt, "14.0m", std::nullopt, "halted_no_silk"});
            return std::vector<ActiveMethodGrant>{{"#02", "companion", "staged", std::move(cands)}};
        });
    crowd_sdk.bind_state_module(
        "crowd_near",
        []() { return JsonObject{{"radar", true}}; },
        []() {
            return std::vector<ActiveMethodGrant>{
                {"#02", "companion", "staged", {
                    {"chest_pointed", "Pointed Chest", "staged", std::nullopt, "9.0m", "pointed_ray", std::nullopt},
                    {"lever_ready", "Close Lever", "ready", std::nullopt, "1.2m", "ahead 1.2m", std::nullopt},
                }},
            };
        });

    crowd_sdk.publish_capabilities_and_baseline();
    const auto& init_sync = std::get<JsonObject>(crowd_posts.back().second.data);
    const auto& upserted_arr = std::get<JsonArray>(init_sync.at("upserted_methods").data);
    assert(upserted_arr.size() == 1);
    const auto& merged_grant = std::get<JsonObject>(upserted_arr[0].data);
    assert(std::get<std::string>(merged_grant.at("readiness").data) == "ready");
    const auto& target_arr = std::get<JsonArray>(merged_grant.at("targets").data);
    assert(target_arr.size() == OriginSdk::kMaxCandidateTargets);
    assert(std::get<std::string>(std::get<JsonObject>(target_arr[0].data).at("target_id").data) == "station_alert");
    assert(std::get<std::string>(std::get<JsonObject>(target_arr[1].data).at("target_id").data) == "chest_pointed");
    assert(std::get<std::string>(std::get<JsonObject>(target_arr[2].data).at("target_id").data) == "lever_ready");

    auto c_exec = crowd_sdk.handle_origin_execute({"slot-crowd-cpp", 1, "invoke", "exec-crowd-1", "#02", "companion", {}});
    assert(c_exec.state == "executing");
    crowd_sdk.report_emergency_stop();
    assert(crowd_step_cancelled);
    assert(crowd_posts.back().first == "origin.player_event");

    crowd_sdk.advance_epoch();
    const auto& ep2_sync = std::get<JsonObject>(crowd_posts.back().second.data);
    assert(std::get<int64_t>(ep2_sync.at("epoch").data) == 2);
    assert(std::get<std::string>(ep2_sync.at("mode").data) == "full");

    // 6. Verify RFC 8259 UTF-16 surrogate pair decoding, lone surrogate rejection, & RFC 8785 §3.2.3 UTF-16 key ordering
    JsonValue parsed_pair = jcs::parse_strict_json(R"({"emoji":"\uD83D\uDE00","\uE000":"bmp","\uD83D\uDE00":"astral"})");
    const auto& pair_obj = std::get<JsonObject>(parsed_pair.data);
    assert(std::get<std::string>(pair_obj.at("emoji").data) == "\xF0\x9F\x98\x80");

    JsonValue astral_order = JsonObject{
        {"\xEE\x80\x80", "bmp"},
        {"\xF0\x9F\x98\x80", "astral"},
    };
    std::string canon_astral = jcs::canonicalize_utf8(astral_order);
    assert(canon_astral == "{\"😀\":\"astral\",\"\xEE\x80\x80\":\"bmp\"}");

    bool caught_lone = false;
    try {
        (void)jcs::parse_strict_json(R"({"bad":"\uD83D"})");
    } catch (const std::invalid_argument&) {
        caught_lone = true;
    }
    assert(caught_lone);

    // 7. Verify required schema argument guard & player_metered independent_npc host_handoff
    std::vector<std::pair<std::string, JsonValue>> handoff_posts;
    OriginSdk boss_sdk(
        "slot-boss-cpp",
        [&](const std::string& op, const JsonValue& p) { handoff_posts.emplace_back(op, p); },
        "independent_npc",
        "player_metered");
    boss_sdk.bind_actor_subject("boss");
    boss_sdk.register_capability(
        {"#07", "cast_aoe", "Cast AOE", {"boss"}, JsonObject{{"required", JsonArray{"sector_id"}}}, false, {}, "allow"},
        [](const std::string&, const JsonObject&, const std::string&) { return EffectOutcome::ImmediateSucceeded(); });

    auto miss_rep = boss_sdk.handle_origin_execute({"slot-boss-cpp", 1, "invoke", "exec-miss-1", "#07", "boss", {}});
    assert(miss_rep.state == "failed" && miss_rep.failure_reason.value_or("") == "missing_required_arg:sector_id");

    boss_sdk.report_npc_host_handoff("teammate-2");
    assert(handoff_posts.back().first == "origin.player_event");
    const auto& ho_obj = std::get<JsonObject>(handoff_posts.back().second.data);
    assert(std::get<std::string>(ho_obj.at("kind").data) == "host_handoff");
    assert(std::get<std::string>(ho_obj.at("new_player_id").data) == "teammate-2");

    // 8. Verify FRAND slot governance fields & reverse-DNS modular extension data (§7.3 & §8.1)
    std::vector<std::pair<std::string, JsonValue>> ext_posts;
    OriginSdk ext_sdk("slot-ext-cpp", [&](const std::string& op, const JsonValue& p) { ext_posts.emplace_back(op, p); });
    ext_sdk.attach_extension_data("org.g2a.sim.batch_blueprint.v1", JsonObject{{"grid", "8x8"}});
    ext_sdk.publish_capabilities_and_baseline(
        "pull://origin.execute",
        false,
        2,
        750,
        {"core-alpha"},
        "https://game.example/g2a-frand");
    const auto& ext_cap = std::get<JsonObject>(ext_posts[0].second.data);
    assert(std::get<int64_t>(ext_cap.at("max_sessions").data) == 2);
    assert(std::get<int64_t>(ext_cap.at("shared_cooldown_ms").data) == 750);
    assert(std::get<std::string>(ext_cap.at("allowlist_criteria_url").data) == "https://game.example/g2a-frand");
    const auto& ext_sync = std::get<JsonObject>(ext_posts[1].second.data);
    const auto& ext_map = std::get<JsonObject>(ext_sync.at("extensions_data").data);
    assert(std::get<std::string>(std::get<JsonObject>(ext_map.at("org.g2a.sim.batch_blueprint.v1").data).at("grid").data) == "8x8");

    // 9. Verify Pose3D 360-degree yaw-relative deictics, recursive JSON Schema bounds, & HTTP/1.1 Keep-Alive listener
    Pose3D player_pose{0.0, 0.0, 0.0, 0.0};
    assert(player_pose.format_player_deictic_anchor({3.0, 0.0, 0.0, 0.0}, false) == "right 3.0m");
    assert(player_pose.format_player_deictic_anchor({0.0, 0.0, 4.0, 0.0}, true) == "pointed_ray 4.0m");

    JsonObject qty_prop{{"type", "integer"}, {"minimum", int64_t(1)}, {"maximum", int64_t(50)}};
    JsonObject strict_schema{
        {"type", "object"},
        {"required", JsonArray{"qty"}},
        {"additionalProperties", false},
        {"properties", JsonObject{{"qty", qty_prop}}},
    };
    ext_sdk.bind_actor_subject("companion");
    ext_sdk.register_capability(
        {"#09", "buy_logs", "Buy logs", {"companion"}, strict_schema, false, {}, "allow"},
        [](const std::string&, const JsonObject&, const std::string&) {
            return EffectOutcome::ImmediateSucceeded(JsonObject{{"bought", true}});
        });
    auto over_max = ext_sdk.handle_origin_execute({"slot-ext-cpp", 1, "invoke", "ex-max", "#09", "companion", JsonObject{{"qty", int64_t(999)}}});
    assert(over_max.state == "failed" && over_max.failure_reason.value_or("") == "above_maximum:qty");

    http::LoopbackExecuteListener listener(ext_sdk, 0, "cpp-ka-tok");
    http::detail::SocketHandle cli = http::detail::connect_ipv4("127.0.0.1", listener.bound_port());
    assert(cli != http::detail::kInvalidSocket);
    std::string ex_body = R"({"g2a_version":"0.0.1","request_id":"c1","timestamp_ms":1,"operation":"origin.execute","payload":{"slot_id":"slot-ext-cpp","epoch":1,"kind":"invoke","execution_id":"ex-ka-ok","method_id":"#09","actor":"companion","args":{"qty":12}},"extensions":{}})";
    std::string r_bad = "POST /g2a/v1/origin/execute HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer wrong\r\nContent-Length: "
                      + std::to_string(ex_body.size()) + "\r\n\r\n" + ex_body;
    assert(http::detail::send_all(cli, r_bad.data(), r_bad.size()));
    std::string lo;
    http::detail::HttpRawMessage m_bad;
    assert(http::detail::read_http_message(cli, lo, m_bad));
    assert(m_bad.first_line.find(" 401 ") != std::string::npos);

    std::string r_ok = "POST /g2a/v1/origin/execute HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer cpp-ka-tok\r\nConnection: close\r\nContent-Length: "
                     + std::to_string(ex_body.size()) + "\r\n\r\n" + ex_body;
    assert(http::detail::send_all(cli, r_ok.data(), r_ok.size()));
    http::detail::HttpRawMessage m_ok;
    assert(http::detail::read_http_message(cli, lo, m_ok));
    assert(m_ok.first_line.find(" 200 ") != std::string::npos);
    assert(m_ok.body.find("\"succeeded\"") != std::string::npos);
    http::detail::close_socket(cli);

    // 10. Verify atomic multi-step batch settlement (complete_steps_batch coalesces into 1 origin.sync delta frame)
    ext_sdk.register_capability(
        {"#10", "patrol_sector", "Sustained patrol", {"companion"}, JsonObject{{"type", "object"}}, true, {}, "allow"},
        [](const std::string&, const JsonObject&, const std::string&) {
            return EffectOutcome::StartedExecuting([]() {});
        });
    assert(ext_sdk.handle_origin_execute({"slot-ext-cpp", 1, "invoke", "ex-b1", "#10", "companion", {}}).state == "executing");
    assert(ext_sdk.handle_origin_execute({"slot-ext-cpp", 1, "invoke", "ex-b2", "#10", "companion", {}}).state == "executing");
    assert(ext_sdk.handle_origin_execute({"slot-ext-cpp", 1, "invoke", "ex-b3", "#10", "companion", {}}).state == "executing");
    size_t before_batch = ext_posts.size();
    size_t settled_cnt = ext_sdk.complete_steps_batch({
        {"ex-b1", true, JsonObject{{"sector", "A"}}, ""},
        {"ex-b2", true, JsonObject{{"sector", "B"}}, ""},
        {"ex-b3", false, {}, "blocked"},
    });
    assert(settled_cnt == 3);
    assert(ext_posts.size() - before_batch == 1);
    const auto& batch_frame = std::get<JsonObject>(ext_posts.back().second.data);
    assert(std::get<JsonArray>(batch_frame.at("settled_executions").data).size() == 3);

    std::cout << "C++17 G2A OriginSdk SelfTest PASSED | JCS=" << digest << "\n";
    return 0;
}
