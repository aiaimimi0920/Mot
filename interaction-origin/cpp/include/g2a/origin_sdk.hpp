#pragma once

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <deque>
#include <fstream>
#include <functional>
#include <map>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "jcs.hpp"

#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#endif

namespace g2a {

struct CapabilityEntry {
    std::string method_id;
    std::string name;
    std::string description;
    std::vector<std::string> permitted_actors;
    JsonObject params_schema;
    bool sustained = false;
    std::vector<std::string> risk_flags;
    std::string default_policy = "allow";

    JsonValue to_json() const {
        JsonArray actors_arr, flags_arr;
        for (const auto& a : permitted_actors) actors_arr.emplace_back(a);
        for (const auto& r : risk_flags) flags_arr.emplace_back(r);
        return JsonObject{
            {"method_id", method_id},
            {"name", name},
            {"description", description},
            {"permitted_actors", actors_arr},
            {"params_schema", params_schema},
            {"sustained", sustained},
            {"risk_flags", flags_arr},
            {"default_policy", default_policy},
        };
    }
};

struct TargetCandidate {
    std::string target_id;
    std::string label;
    std::string readiness; // "ready" | "staged"
    std::optional<std::string> prerequisite_hint;
    std::optional<std::string> actor_anchor;
    std::optional<std::string> player_anchor;
    std::optional<std::string> alert_tag;

    JsonValue to_json() const {
        JsonObject o{
            {"target_id", target_id},
            {"label", label},
            {"readiness", readiness},
        };
        if (prerequisite_hint) o["prerequisite_hint"] = *prerequisite_hint;
        if (actor_anchor) o["actor_anchor"] = *actor_anchor;
        if (player_anchor) o["player_anchor"] = *player_anchor;
        if (alert_tag) o["alert_tag"] = *alert_tag;
        return o;
    }
};

struct ActiveMethodGrant {
    std::string method_id;
    std::string actor;
    std::string readiness;
    std::vector<TargetCandidate> targets;

    JsonValue to_json() const {
        JsonArray t_arr;
        for (const auto& t : targets) t_arr.push_back(t.to_json());
        return JsonObject{
            {"method_id", method_id},
            {"actor", actor},
            {"readiness", readiness},
            {"targets", t_arr},
        };
    }
};

struct OriginExecuteRequest {
    std::string slot_id;
    int64_t epoch = 1;
    std::string kind = "invoke"; // "invoke" | "cancel" | "deliver_chat"
    std::string execution_id;
    std::optional<std::string> method_id;
    std::optional<std::string> actor;
    JsonObject args;
};

struct OriginExecuteReply {
    std::string execution_id;
    std::string state; // "executing" | "succeeded" | "failed" | "cancelled"
    std::optional<JsonObject> effect_summary;
    std::optional<std::string> failure_reason;

    JsonValue to_json() const {
        JsonObject o{
            {"execution_id", execution_id},
            {"state", state},
        };
        if (effect_summary) o["effect_summary"] = *effect_summary;
        if (failure_reason) o["failure_reason"] = *failure_reason;
        return o;
    }
};

struct EffectOutcome {
    std::string kind; // "succeeded" | "failed" | "executing"
    JsonObject summary;
    std::string failure_reason;
    std::function<void()> cancel_fn;

    static EffectOutcome ImmediateSucceeded(JsonObject s = {}) { return {"succeeded", std::move(s), "", nullptr}; }
    static EffectOutcome ImmediateFailed(std::string reason) { return {"failed", {}, std::move(reason), nullptr}; }
    static EffectOutcome StartedExecuting(std::function<void()> cancel) { return {"executing", {}, "", std::move(cancel)}; }
};

struct Pose3D {
    double x = 0.0;
    double y = 0.0;
    double z = 0.0;
    double yaw_deg = 0.0;

    double distance_to(const Pose3D& o) const {
        double dx = o.x - x, dy = o.y - y, dz = o.z - z;
        return std::sqrt(dx * dx + dy * dy + dz * dz);
    }

    // Formats a 360-degree yaw-relative player deictic anchor ("ahead 2.4m", "front-right 4.0m", "pointed_ray 3.1m").
    std::string format_player_deictic_anchor(const Pose3D& target, bool is_pointed = false) const {
        double dist = distance_to(target);
        char dist_buf[32];
        std::snprintf(dist_buf, sizeof(dist_buf), "%.1fm", dist);
        if (is_pointed) return std::string("pointed_ray ") + dist_buf;
        double dx = target.x - x;
        double dz = target.z - z;
        if (dx * dx + dz * dz < 1e-6) return std::string("here ") + dist_buf;
        constexpr double kRadToDeg = 180.0 / 3.14159265358979323846;
        double world_deg = std::atan2(dx, dz) * kRadToDeg;
        double rel = std::fmod(world_deg - yaw_deg, 360.0);
        if (rel > 180.0) rel -= 360.0;
        else if (rel <= -180.0) rel += 360.0;

        const char* sector = "behind";
        if (rel >= -22.5 && rel <= 22.5) sector = "ahead";
        else if (rel > 22.5 && rel <= 67.5) sector = "front-right";
        else if (rel > 67.5 && rel <= 112.5) sector = "right";
        else if (rel > 112.5 && rel <= 157.5) sector = "back-right";
        else if (rel >= -67.5 && rel < -22.5) sector = "front-left";
        else if (rel >= -112.5 && rel < -67.5) sector = "left";
        else if (rel >= -157.5 && rel < -112.5) sector = "back-left";
        return std::string(sector) + " " + dist_buf;
    }
};

namespace schema_detail {

inline std::optional<double> as_num(const JsonValue& v) {
    if (const auto* iv = std::get_if<int64_t>(&v.data)) return static_cast<double>(*iv);
    if (const auto* fv = std::get_if<double>(&v.data)) return *fv;
    return std::nullopt;
}

inline std::optional<int64_t> as_int(const JsonValue& v) {
    if (const auto* iv = std::get_if<int64_t>(&v.data)) return *iv;
    if (const auto* fv = std::get_if<double>(&v.data)) {
        if (std::isfinite(*fv) && std::floor(*fv) == *fv) return static_cast<int64_t>(*fv);
    }
    return std::nullopt;
}

inline std::string check_bounds(const std::string& name, double val, const JsonObject& spec) {
    if (auto it = spec.find("minimum"); it != spec.end()) {
        if (auto m = as_num(it->second); m && val < *m) return "below_minimum:" + name;
    }
    if (auto it = spec.find("maximum"); it != spec.end()) {
        if (auto m = as_num(it->second); m && val > *m) return "above_maximum:" + name;
    }
    if (auto it = spec.find("exclusiveMinimum"); it != spec.end()) {
        if (auto m = as_num(it->second); m && val <= *m) return "below_exclusive_minimum:" + name;
    }
    if (auto it = spec.find("exclusiveMaximum"); it != spec.end()) {
        if (auto m = as_num(it->second); m && val >= *m) return "above_exclusive_maximum:" + name;
    }
    return "";
}

inline std::string validate_object(const JsonObject& args, const JsonObject& schema, const std::string& prefix, int depth);

inline std::string validate_field(const std::string& name, const JsonValue& val, const JsonObject& spec, int depth) {
    if (depth > 16) return "schema_depth_exceeded";
    auto t_it = spec.find("type");
    if (t_it == spec.end()) return "";
    const auto* ptype = std::get_if<std::string>(&t_it->second.data);
    if (!ptype) return "";

    if (*ptype == "string") {
        const auto* s = std::get_if<std::string>(&val.data);
        if (!s) return "invalid_type:" + name;
        if (auto it = spec.find("minLength"); it != spec.end()) {
            if (auto ml = as_int(it->second); ml && static_cast<int64_t>(s->size()) < *ml) return "min_length:" + name;
        }
        if (auto it = spec.find("maxLength"); it != spec.end()) {
            if (auto ml = as_int(it->second); ml && static_cast<int64_t>(s->size()) > *ml) return "max_length:" + name;
        }
        if (auto it = spec.find("enum"); it != spec.end()) {
            if (const auto* arr = std::get_if<JsonArray>(&it->second.data)) {
                bool matched = false;
                for (const auto& e : *arr) {
                    if (const auto* es = std::get_if<std::string>(&e.data); es && *es == *s) {
                        matched = true;
                        break;
                    }
                }
                if (!matched) return "invalid_enum:" + name;
            }
        }
    } else if (*ptype == "boolean") {
        if (!std::holds_alternative<bool>(val.data)) return "invalid_type:" + name;
    } else if (*ptype == "integer") {
        auto iv = as_int(val);
        if (!iv) return "invalid_type:" + name;
        return check_bounds(name, static_cast<double>(*iv), spec);
    } else if (*ptype == "number") {
        auto fv = as_num(val);
        if (!fv || !std::isfinite(*fv)) return "invalid_type:" + name;
        return check_bounds(name, *fv, spec);
    } else if (*ptype == "array") {
        const auto* arr = std::get_if<JsonArray>(&val.data);
        if (!arr) return "invalid_type:" + name;
        if (auto it = spec.find("minItems"); it != spec.end()) {
            if (auto mi = as_int(it->second); mi && static_cast<int64_t>(arr->size()) < *mi) return "min_items:" + name;
        }
        if (auto it = spec.find("maxItems"); it != spec.end()) {
            if (auto ma = as_int(it->second); ma && static_cast<int64_t>(arr->size()) > *ma) return "max_items:" + name;
        }
        if (auto it = spec.find("items"); it != spec.end()) {
            if (const auto* item_spec = std::get_if<JsonObject>(&it->second.data)) {
                for (size_t i = 0; i < arr->size(); ++i) {
                    std::string err = validate_field(name + "[" + std::to_string(i) + "]", (*arr)[i], *item_spec, depth + 1);
                    if (!err.empty()) return err;
                }
            }
        }
    } else if (*ptype == "object") {
        const auto* sub = std::get_if<JsonObject>(&val.data);
        if (!sub) return "invalid_type:" + name;
        return validate_object(*sub, spec, name, depth + 1);
    }
    return "";
}

inline std::string validate_object(const JsonObject& args, const JsonObject& schema, const std::string& prefix, int depth) {
    if (depth > 16) return "schema_depth_exceeded";
    if (schema.empty()) return "";
    if (auto req_it = schema.find("required"); req_it != schema.end()) {
        if (const auto* req_arr = std::get_if<JsonArray>(&req_it->second.data)) {
            for (const auto& item : *req_arr) {
                if (const auto* k = std::get_if<std::string>(&item.data)) {
                    if (args.find(*k) == args.end()) {
                        return "missing_required_arg:" + (prefix.empty() ? *k : prefix + "." + *k);
                    }
                }
            }
        }
    }
    if (auto prop_it = schema.find("properties"); prop_it != schema.end()) {
        if (const auto* props = std::get_if<JsonObject>(&prop_it->second.data)) {
            bool no_extra = false;
            if (auto ap_it = schema.find("additionalProperties"); ap_it != schema.end()) {
                if (const auto* b = std::get_if<bool>(&ap_it->second.data)) no_extra = (*b == false);
            }
            if (no_extra) {
                for (const auto& [k, _] : args) {
                    if (props->find(k) == props->end()) {
                        return "unexpected_arg:" + (prefix.empty() ? k : prefix + "." + k);
                    }
                }
            }
            for (const auto& [k, v] : args) {
                if (auto s_it = props->find(k); s_it != props->end()) {
                    if (const auto* spec = std::get_if<JsonObject>(&s_it->second.data)) {
                        std::string qual = prefix.empty() ? k : prefix + "." + k;
                        std::string err = validate_field(qual, v, *spec, depth + 1);
                        if (!err.empty()) return err;
                    }
                }
            }
        }
    }
    return "";
}

} // namespace schema_detail

using TransportPostFn = std::function<void(const std::string& operation, const JsonValue& payload)>;
using DomainEffectFn = std::function<EffectOutcome(const std::string& actor, const JsonObject& args, const std::string& exec_id)>;

// Header-only C++17 Interaction Origin SDK for Unreal Engine & Custom C++ Game Hosts (G2A V0.0.1).
class OriginSdk {
public:
    static constexpr const char* kWireVersion = "0.0.1";
    static constexpr size_t kMaxCandidateTargets = 24;
    static constexpr size_t kMaxExecutionLedger = 512;

    OriginSdk(
        std::string slot_id,
        TransportPostFn transport,
        std::string role_kind = "companion",
        std::string billing_mode = "player_metered",
        std::string room_id = "",
        std::string faction_id = "")
        : slot_id_(std::move(slot_id)),
          role_kind_(std::move(role_kind)),
          billing_mode_(std::move(billing_mode)),
          room_id_(std::move(room_id)),
          faction_id_(std::move(faction_id)),
          transport_(std::move(transport)) {}

    static std::string discover_local_hub_origin_url(const std::string& explicit_manifest_path = "") {
        std::string path = explicit_manifest_path;
        if (path.empty()) {
            if (const char* env = std::getenv("G2A_HUB_RUNTIME_FILE"); env && *env) {
                path = env;
            } else if (const char* local_app = std::getenv("LOCALAPPDATA"); local_app && *local_app) {
                path = std::string(local_app) + "\\G2A\\hub.runtime.json";
            } else if (const char* home = std::getenv("HOME"); home && *home) {
                path = std::string(home) + "/.g2a/hub.runtime.json";
            }
        }
        std::ifstream ifs(path);
        if (!ifs) throw std::runtime_error("Local Hub runtime manifest not found: " + path);
        std::ostringstream ss;
        ss << ifs.rdbuf();
        JsonValue root = jcs::parse_strict_json(ss.str());
        const auto* obj = std::get_if<JsonObject>(&root.data);
        if (!obj || obj->find("origin_url") == obj->end()) {
            throw std::runtime_error("Missing origin_url in Hub runtime manifest");
        }
        return std::get<std::string>(obj->at("origin_url").data);
    }

    void register_capability(CapabilityEntry def, DomainEffectFn handler) {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        if (role_kind_ == "independent_npc") {
            def.permitted_actors.erase(
                std::remove(def.permitted_actors.begin(), def.permitted_actors.end(), "player"),
                def.permitted_actors.end());
        }
        std::string id = def.method_id;
        capabilities_[id] = {std::move(def), std::move(handler)};
    }

    void bind_actor_subject(const std::string& actor) {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        actors_[actor] = true;
    }

    void bind_state_module(
        const std::string& name,
        std::function<JsonObject()> snapshot_fn,
        std::function<std::vector<ActiveMethodGrant>()> grants_fn) {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        modules_[name] = {std::move(snapshot_fn), std::move(grants_fn)};
    }

    void attach_extension_data(const std::string& extension_name, JsonObject data, bool flush = false) {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        extensions_data_[extension_name] = std::move(data);
        if (flush) flush_dirty_sync(false);
    }

    void publish_capabilities_and_baseline(
        const std::string& execute_url = "",
        bool disabled = false,
        int64_t max_sessions = 0,
        int64_t shared_cooldown_ms = 0,
        const std::vector<std::string>& allowlisted_cores = {},
        const std::string& allowlist_criteria_url = "") {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        JsonArray defs;
        for (const auto& [_, reg] : capabilities_) {
            defs.push_back(reg.def.to_json());
        }
        std::string cap_digest = jcs::digest_sha256("g2a.cap.v1", defs);
        JsonObject cap_payload{
            {"slot_id", slot_id_},
            {"role_kind", role_kind_},
            {"billing_mode", billing_mode_},
            {"origin_execute_url", execute_url},
            {"capability_table_digest", cap_digest},
            {"capabilities", defs},
        };
        if (disabled) cap_payload["disabled"] = true;
        if (max_sessions > 0) cap_payload["max_sessions"] = max_sessions;
        if (shared_cooldown_ms > 0) cap_payload["shared_cooldown_ms"] = shared_cooldown_ms;
        if (!allowlisted_cores.empty()) {
            JsonArray allow_arr;
            for (const auto& c : allowlisted_cores) allow_arr.emplace_back(c);
            cap_payload["allowlisted_cores"] = std::move(allow_arr);
        }
        if (!allowlist_criteria_url.empty()) cap_payload["allowlist_criteria_url"] = allowlist_criteria_url;
        if (!room_id_.empty()) cap_payload["room_id"] = room_id_;
        if (!faction_id_.empty()) cap_payload["faction_id"] = faction_id_;
        transport_("origin.capabilities", cap_payload);
        flush_full_sync();
    }

    bool flush_dirty_sync(bool significant_event = false) {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        if (!sync_enabled_ && pending_settled_.empty()) return false;

        auto [brief, grants] = collect_view();
        JsonValue digest_doc = brief;
        JsonObject ext_obj;
        if (!extensions_data_.empty()) {
            for (const auto& [k, v] : extensions_data_) ext_obj.emplace(k, v);
            JsonObject combined;
            combined.emplace("brief", brief);
            combined.emplace("ext", ext_obj);
            digest_doc = std::move(combined);
        }
        std::string brief_digest = jcs::digest_sha256("g2a.brief.v1", digest_doc);

        std::map<std::pair<std::string, std::string>, std::string> cur_canon;
        JsonArray upserted, removed;
        for (const auto& [key, g] : grants) {
            JsonValue g_json = g.to_json();
            std::string canon = jcs::canonicalize_utf8(g_json);
            cur_canon[key] = canon;
            auto it = last_sent_methods_.find(key);
            if (it == last_sent_methods_.end() || it->second != canon) {
                upserted.push_back(std::move(g_json));
            }
        }
        for (const auto& [key, _] : last_sent_methods_) {
            if (cur_canon.find(key) == cur_canon.end()) {
                JsonObject rem_entry;
                rem_entry.emplace("method_id", key.first);
                rem_entry.emplace("actor", key.second);
                removed.emplace_back(std::move(rem_entry));
            }
        }

        JsonArray settled_arr;
        for (const auto& s : pending_settled_) settled_arr.push_back(s.to_json());
        pending_settled_.clear();

        if (brief_digest == last_brief_digest_ && upserted.empty() && removed.empty() && settled_arr.empty()) {
            return false;
        }

        int64_t prev_rev = current_rev_++;
        last_brief_digest_ = brief_digest;
        last_sent_methods_ = std::move(cur_canon);

        JsonObject frame{
            {"slot_id", slot_id_},
            {"epoch", current_epoch_},
            {"rev", current_rev_},
            {"base_rev", prev_rev},
            {"mode", "delta"},
            {"significant_event", significant_event},
            {"state_brief", brief},
            {"upserted_methods", upserted},
            {"removed_methods", removed},
            {"settled_executions", settled_arr},
        };
        if (!ext_obj.empty()) frame.emplace("extensions_data", std::move(ext_obj));
        transport_("origin.sync", frame);
        return true;
    }

    void flush_full_sync() {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        auto [brief, grants] = collect_view();
        current_rev_++;
        JsonValue digest_doc = brief;
        JsonObject ext_obj;
        if (!extensions_data_.empty()) {
            for (const auto& [k, v] : extensions_data_) ext_obj.emplace(k, v);
            JsonObject combined;
            combined.emplace("brief", brief);
            combined.emplace("ext", ext_obj);
            digest_doc = std::move(combined);
        }
        last_brief_digest_ = jcs::digest_sha256("g2a.brief.v1", digest_doc);
        last_sent_methods_.clear();
        JsonArray upserted;
        for (const auto& [key, g] : grants) {
            JsonValue g_json = g.to_json();
            last_sent_methods_[key] = jcs::canonicalize_utf8(g_json);
            upserted.push_back(std::move(g_json));
        }
        JsonArray settled_arr;
        for (const auto& s : pending_settled_) settled_arr.push_back(s.to_json());
        pending_settled_.clear();

        JsonObject frame{
            {"slot_id", slot_id_},
            {"epoch", current_epoch_},
            {"rev", current_rev_},
            {"mode", "full"},
            {"significant_event", true},
            {"state_brief", brief},
            {"upserted_methods", upserted},
            {"removed_methods", JsonArray{}},
            {"settled_executions", settled_arr},
        };
        if (!ext_obj.empty()) frame.emplace("extensions_data", std::move(ext_obj));
        transport_("origin.sync", frame);
    }

    OriginExecuteReply handle_origin_execute(const OriginExecuteRequest& req) {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        if (req.kind == "cancel") {
            return cancel_in_flight_step(req.execution_id);
        }
        if (req.kind == "deliver_chat") {
            return {req.execution_id, "succeeded", std::nullopt, std::nullopt};
        }
        auto led_it = ledger_.find(req.execution_id);
        if (led_it != ledger_.end()) return led_it->second;

        if (req.epoch != current_epoch_ || !req.method_id || !req.actor) {
            return {req.execution_id, "failed", std::nullopt, "invalid_epoch_or_subject"};
        }
        auto cap_it = capabilities_.find(*req.method_id);
        if (cap_it == capabilities_.end() || actors_.find(*req.actor) == actors_.end()) {
            return {req.execution_id, "failed", std::nullopt, "unknown_method_or_actor"};
        }
        const auto& allowed = cap_it->second.def.permitted_actors;
        if (std::find(allowed.begin(), allowed.end(), *req.actor) == allowed.end()) {
            return {req.execution_id, "failed", std::nullopt, "permission_denied"};
        }
        std::string schema_err = schema_detail::validate_object(req.args, cap_it->second.def.params_schema, "", 0);
        if (!schema_err.empty()) {
            return {req.execution_id, "failed", std::nullopt, schema_err};
        }

        EffectOutcome out = cap_it->second.handler(*req.actor, req.args, req.execution_id);
        if (out.kind == "executing") {
            in_flight_[req.execution_id] = std::move(out.cancel_fn);
            OriginExecuteReply rep{req.execution_id, "executing", std::nullopt, std::nullopt};
            store_ledger_reply(rep);
            return rep;
        }
        OriginExecuteReply rep{
            req.execution_id,
            out.kind,
            out.kind == "succeeded" ? std::optional<JsonObject>(out.summary) : std::nullopt,
            out.kind == "failed" ? std::optional<std::string>(out.failure_reason) : std::nullopt,
        };
        store_ledger_reply(rep);
        flush_dirty_sync(false);
        return rep;
    }

    void complete_step(const std::string& execution_id, bool succeeded, JsonObject summary = {}, std::string reason = "") {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        if (in_flight_.erase(execution_id) == 0) return;
        OriginExecuteReply rep{
            execution_id,
            succeeded ? "succeeded" : "failed",
            succeeded ? std::optional<JsonObject>(std::move(summary)) : std::nullopt,
            succeeded ? std::nullopt : std::optional<std::string>(std::move(reason)),
        };
        store_ledger_reply(rep);
        pending_settled_.push_back(std::move(rep));
        flush_dirty_sync(false);
    }

    OriginExecuteReply cancel_in_flight_step(const std::string& execution_id) {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        auto it = in_flight_.find(execution_id);
        if (it != in_flight_.end()) {
            auto fn = std::move(it->second);
            in_flight_.erase(it);
            if (fn) fn();
            OriginExecuteReply rep{execution_id, "cancelled", std::nullopt, "cancelled_by_hub"};
            store_ledger_reply(rep);
            pending_settled_.push_back(rep);
            flush_dirty_sync(false);
            return rep;
        }
        auto led_it = ledger_.find(execution_id);
        if (led_it != ledger_.end()) return led_it->second;
        return {execution_id, "cancelled", std::nullopt, std::nullopt};
    }

    void advance_epoch() {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        current_epoch_++;
        current_rev_ = 0;
        last_sent_methods_.clear();
        cancel_all_in_flight("epoch_advanced");
        flush_full_sync();
    }

    void report_player_command(const std::string& command_text) {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        flush_dirty_sync(true);
        transport_("origin.player_event", JsonObject{
            {"slot_id", slot_id_},
            {"kind", "player_command"},
            {"text", command_text},
        });
    }

    void report_player_presence(bool online, int64_t remaining_online_humans = 0) {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        sync_enabled_ = online || remaining_online_humans > 0;
        transport_("origin.player_event", JsonObject{
            {"slot_id", slot_id_},
            {"kind", "presence_changed"},
            {"online", online},
            {"remaining_online_humans", remaining_online_humans},
        });
    }

    void report_emergency_stop() {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        cancel_all_in_flight("player_emergency_stop");
        flush_dirty_sync(false);
        transport_("origin.player_event", JsonObject{
            {"slot_id", slot_id_},
            {"kind", "emergency_stop"},
        });
    }

    void report_queue_approval(
        const std::string& session_id,
        const std::string& queue_id,
        const std::string& queue_digest,
        const std::string& fingerprint,
        bool approved) {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        transport_("origin.player_event", JsonObject{
            {"slot_id", slot_id_},
            {"kind", "queue_approval"},
            {"session_id", session_id},
            {"queue_id", queue_id},
            {"queue_digest", queue_digest},
            {"fingerprint", fingerprint},
            {"approved", approved},
        });
    }

    void report_npc_host_handoff(const std::string& new_player_id) {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        if (role_kind_ != "independent_npc" || billing_mode_ != "player_metered") {
            throw std::logic_error("host_handoff is only permitted on player_metered independent_npc slots");
        }
        if (new_player_id.empty()) {
            throw std::invalid_argument("new_player_id is required");
        }
        cancel_all_in_flight("npc_host_handoff");
        sync_enabled_ = true;
        flush_dirty_sync(false);
        transport_("origin.player_event", JsonObject{
            {"slot_id", slot_id_},
            {"kind", "host_handoff"},
            {"new_player_id", new_player_id},
        });
    }

    const CapabilityEntry& get_capability(const std::string& id) const { return capabilities_.at(id).def; }

private:
    struct RegCap {
        CapabilityEntry def;
        DomainEffectFn handler;
    };
    struct StateMod {
        std::function<JsonObject()> snapshot_fn;
        std::function<std::vector<ActiveMethodGrant>()> grants_fn;
    };

    void store_ledger_reply(const OriginExecuteReply& rep) {
        if (ledger_.find(rep.execution_id) == ledger_.end()) {
            ledger_order_.push_back(rep.execution_id);
        }
        ledger_[rep.execution_id] = rep;
        size_t guard = ledger_order_.size();
        while (ledger_.size() > kMaxExecutionLedger && guard-- > 0) {
            std::string oldest = std::move(ledger_order_.front());
            ledger_order_.pop_front();
            if (in_flight_.find(oldest) != in_flight_.end()) {
                ledger_order_.push_back(std::move(oldest));
            } else {
                ledger_.erase(oldest);
            }
        }
    }

    void cancel_all_in_flight(const std::string& reason) {
        std::vector<std::pair<std::string, std::function<void()>>> active;
        active.reserve(in_flight_.size());
        for (auto& kv : in_flight_) {
            active.emplace_back(kv.first, std::move(kv.second));
        }
        in_flight_.clear();
        for (auto& [exec_id, fn] : active) {
            if (fn) fn();
            OriginExecuteReply rep{exec_id, "cancelled", std::nullopt, reason};
            store_ledger_reply(rep);
            pending_settled_.push_back(std::move(rep));
        }
    }

    static double parse_anchor_meters(const std::optional<std::string>& anchor) {
        if (!anchor || anchor->empty()) return 1e9;
        std::string s = *anchor;
        while (!s.empty() && std::isspace(static_cast<unsigned char>(s.back()))) s.pop_back();
        if (s.empty() || s.back() != 'm') return 1e9;
        s.pop_back();
        size_t sp = s.rfind(' ');
        std::string tok = (sp == std::string::npos) ? s : s.substr(sp + 1);
        try {
            size_t idx = 0;
            double d = std::stod(tok, &idx);
            return (idx == tok.size() && std::isfinite(d)) ? d : 1e9;
        } catch (...) {
            return 1e9;
        }
    }

    static bool contains_pointed_ci(const std::optional<std::string>& anchor) {
        if (!anchor) return false;
        std::string low;
        low.reserve(anchor->size());
        for (char c : *anchor) {
            low.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));
        }
        return low.find("pointed") != std::string::npos;
    }

    static std::vector<TargetCandidate> prioritize_targets(const std::vector<TargetCandidate>& raw) {
        std::map<std::string, TargetCandidate> dedup;
        for (const auto& t : raw) {
            auto it = dedup.find(t.target_id);
            if (it == dedup.end() || t.readiness == "ready") {
                dedup[t.target_id] = t;
            }
        }
        std::vector<TargetCandidate> ordered;
        ordered.reserve(dedup.size());
        for (auto& kv : dedup) ordered.push_back(std::move(kv.second));

        std::sort(ordered.begin(), ordered.end(), [](const TargetCandidate& a, const TargetCandidate& b) {
            int a_alert = (a.alert_tag && !a.alert_tag->empty()) ? 0 : 1;
            int b_alert = (b.alert_tag && !b.alert_tag->empty()) ? 0 : 1;
            if (a_alert != b_alert) return a_alert < b_alert;
            int a_pt = contains_pointed_ci(a.player_anchor) ? 0 : 1;
            int b_pt = contains_pointed_ci(b.player_anchor) ? 0 : 1;
            if (a_pt != b_pt) return a_pt < b_pt;
            int a_rdy = (a.readiness == "ready") ? 0 : 1;
            int b_rdy = (b.readiness == "ready") ? 0 : 1;
            if (a_rdy != b_rdy) return a_rdy < b_rdy;
            double d_diff = parse_anchor_meters(a.actor_anchor) - parse_anchor_meters(b.actor_anchor);
            if (std::fabs(d_diff) > 1e-6) return d_diff < 0.0;
            return a.target_id < b.target_id;
        });

        if (ordered.size() > kMaxCandidateTargets) {
            ordered.resize(kMaxCandidateTargets);
        }
        return ordered;
    }

    static void merge_grant(
        std::map<std::pair<std::string, std::string>, ActiveMethodGrant>& grants,
        const ActiveMethodGrant& incoming) {
        std::pair<std::string, std::string> key{incoming.method_id, incoming.actor};
        auto it = grants.find(key);
        if (it != grants.end()) {
            std::vector<TargetCandidate> combined = it->second.targets;
            combined.insert(combined.end(), incoming.targets.begin(), incoming.targets.end());
            auto prioritized = prioritize_targets(combined);
            bool any_ready = (it->second.readiness == "ready") || (incoming.readiness == "ready");
            for (const auto& t : prioritized) {
                if (t.readiness == "ready") {
                    any_ready = true;
                    break;
                }
            }
            it->second = ActiveMethodGrant{
                incoming.method_id,
                incoming.actor,
                any_ready ? "ready" : "staged",
                std::move(prioritized),
            };
        } else {
            grants[key] = ActiveMethodGrant{
                incoming.method_id,
                incoming.actor,
                incoming.readiness,
                prioritize_targets(incoming.targets),
            };
        }
    }

    std::pair<JsonObject, std::map<std::pair<std::string, std::string>, ActiveMethodGrant>> collect_view() {
        JsonObject brief;
        std::map<std::pair<std::string, std::string>, ActiveMethodGrant> grants;
        for (const auto& [name, mod] : modules_) {
            brief[name] = mod.snapshot_fn();
            for (const auto& g : mod.grants_fn()) {
                merge_grant(grants, g);
            }
        }
        return {std::move(brief), std::move(grants)};
    }

    std::string slot_id_;
    std::string role_kind_;
    std::string billing_mode_;
    std::string room_id_;
    std::string faction_id_;
    TransportPostFn transport_;
    mutable std::recursive_mutex mu_;

    int64_t current_epoch_ = 1;
    int64_t current_rev_ = 0;
    bool sync_enabled_ = true;

    std::map<std::string, RegCap> capabilities_;
    std::map<std::string, bool> actors_;
    std::map<std::string, StateMod> modules_;
    std::map<std::string, JsonObject> extensions_data_;
    std::map<std::string, std::function<void()>> in_flight_;
    std::map<std::string, OriginExecuteReply> ledger_;
    std::deque<std::string> ledger_order_;
    std::vector<OriginExecuteReply> pending_settled_;
    std::map<std::pair<std::string, std::string>, std::string> last_sent_methods_;
    std::string last_brief_digest_;
};

} // namespace g2a

#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic pop
#endif
