#pragma once

#include <algorithm>
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

    void publish_capabilities_and_baseline(const std::string& execute_url = "") {
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
        if (!room_id_.empty()) cap_payload["room_id"] = room_id_;
        if (!faction_id_.empty()) cap_payload["faction_id"] = faction_id_;
        transport_("origin.capabilities", cap_payload);
        flush_full_sync();
    }

    bool flush_dirty_sync(bool significant_event = false) {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        if (!sync_enabled_ && pending_settled_.empty()) return false;

        auto [brief, grants] = collect_view();
        std::string brief_digest = jcs::digest_sha256("g2a.brief.v1", brief);

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
                removed.push_back(JsonObject{{"method_id", key.first}, {"actor", key.second}});
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

        transport_("origin.sync", JsonObject{
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
        });
        return true;
    }

    void flush_full_sync() {
        std::lock_guard<std::recursive_mutex> lk(mu_);
        auto [brief, grants] = collect_view();
        current_rev_++;
        last_brief_digest_ = jcs::digest_sha256("g2a.brief.v1", brief);
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

        transport_("origin.sync", JsonObject{
            {"slot_id", slot_id_},
            {"epoch", current_epoch_},
            {"rev", current_rev_},
            {"mode", "full"},
            {"significant_event", true},
            {"state_brief", brief},
            {"upserted_methods", upserted},
            {"removed_methods", JsonArray{}},
            {"settled_executions", settled_arr},
        });
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

    std::pair<JsonObject, std::map<std::pair<std::string, std::string>, ActiveMethodGrant>> collect_view() {
        JsonObject brief;
        std::map<std::pair<std::string, std::string>, ActiveMethodGrant> grants;
        for (const auto& [name, mod] : modules_) {
            brief[name] = mod.snapshot_fn();
            for (const auto& g : mod.grants_fn()) {
                grants[{g.method_id, g.actor}] = g;
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
    std::map<std::string, std::function<void()>> in_flight_;
    std::map<std::string, OriginExecuteReply> ledger_;
    std::deque<std::string> ledger_order_;
    std::vector<OriginExecuteReply> pending_settled_;
    std::map<std::pair<std::string, std::string>, std::string> last_sent_methods_;
    std::string last_brief_digest_;
};

} // namespace g2a
