class_name G2AOriginSDK
extends Node
## Tier 1 Interaction Origin SDK for Godot 4.x (G2A V0.0.1).
## Supports Signal-Snapshot Modules (e.g. wallet.gd / crafting.gd `signal changed` + `snapshot()`)
## and Embodied 3D/XR Spatial Dirty-Diffs, invoking domain effects directly without hotkey animation locks.
## Uses a single reusable FIFO HTTPRequest worker to guarantee strict rev ordering and zero Node allocation churn.

const G2A_WIRE_VERSION := "0.0.1"
const MAX_CANDIDATE_TARGETS := 24
const MAX_EXECUTION_LEDGER := 512

@export var slot_id: String = "slot-companion"
@export_enum("companion", "independent_npc") var slot_role_kind: String = "companion"
@export_enum("player_metered", "developer_sponsored") var billing_mode: String = "player_metered"
@export var hub_origin_url: String = "http://127.0.0.1:19210"
@export var origin_token: String = ""
@export var room_id: String = ""
@export var faction_id: String = ""
@export var perception_radius: float = 15.0
@export var slot_disabled: bool = false
@export var max_sessions: int = 0
@export var shared_cooldown_ms: int = 0
@export var allowlisted_cores: PackedStringArray = PackedStringArray()
@export var allowlist_criteria_url: String = ""

signal ai_chat_delivered(audience: String, text: String)

var current_epoch: int = 1
var current_rev: int = 0
var sync_enabled: bool = true

var _capabilities: Dictionary = {}        # method_id -> Dictionary { def, effect_callable, readiness_callable }
var _actors: Dictionary = {}              # actor -> Variant (Node3D / RefCounted seat controller)
var _signal_modules: Dictionary = {}      # module_name -> Dictionary { snapshot_fn, active_methods_fn }
var _extensions_data: Dictionary = {}     # extension_name -> Dictionary
var _visual_ref: Variant = null           # Optional Dictionary
var _audio_ref: Variant = null            # Optional Dictionary
var _allowed_timeline_tags: Variant = null # Optional Array[String]
var frames_synced: int = 0
var zero_change_skips: int = 0
var _in_flight_steps: Dictionary = {}     # execution_id -> Callable (cancel_fn)
var _execution_ledger: Dictionary = {}    # execution_id -> Dictionary (OriginExecuteReply)
var _ledger_order: Array[String] = []
var _pending_settled: Array = []          # Array[Dictionary]
var _last_sent_methods: Dictionary = {}   # "method_id|actor" -> String (canonical JSON)
var _last_sent_brief_digest: String = ""

var _http_worker: HTTPRequest = null
var _http_busy: bool = false
var _outbound_queue: Array[Dictionary] = []

func _ready() -> void:
	_ensure_http_worker()

func _exit_tree() -> void:
	if sync_enabled:
		report_player_presence(false, 0)

## One-step Godot 4 host bootstrap: discovers local Hub manifest (falling back to hub_origin_url),
## binds default companion/player subjects if unset, and publishes capabilities + initial baseline.
func start_quick_host(origin_execute_url: String = "pull://origin.execute") -> void:
	auto_discover_local_hub()
	if not _actors.has("companion"):
		bind_actor_subject("companion", self)
	if slot_role_kind != "independent_npc" and not _actors.has("player"):
		bind_actor_subject("player", self)
	publish_capabilities_and_baseline(origin_execute_url)

func _ensure_http_worker() -> void:
	if _http_worker != null or not is_inside_tree():
		return
	_http_worker = HTTPRequest.new()
	_http_worker.use_threads = true
	add_child(_http_worker)
	_http_worker.request_completed.connect(_on_worker_completed)

func _on_worker_completed(_result: int, _code: int, _headers: PackedStringArray, _body: PackedByteArray) -> void:
	_http_busy = false
	_pump_outbound_queue()

func register_capability(def: Dictionary, effect_handler: Callable, readiness_check: Callable = Callable()) -> void:
	var copy_def := def.duplicate(true)
	if slot_role_kind == "independent_npc":
		var filtered: Array = []
		for a in copy_def.get("permitted_actors", []):
			if a != "player":
				filtered.append(a)
		copy_def["permitted_actors"] = filtered
	_capabilities[copy_def["method_id"]] = {
		"def": copy_def,
		"effect_handler": effect_handler,
		"readiness_check": readiness_check,
	}

func bind_actor_subject(actor: String, instance: Variant) -> void:
	_actors[actor] = instance

## Discovers the locally running Intelligent Hub via `hub.runtime.json` without hardcoding ports.
func auto_discover_local_hub() -> bool:
	var path := OS.get_environment("G2A_HUB_RUNTIME_FILE")
	if path == "":
		if OS.get_name() == "Windows":
			var local_app := OS.get_environment("LOCALAPPDATA")
			if local_app != "":
				path = local_app.path_join("G2A/hub.runtime.json")
		if path == "":
			path = OS.get_environment("HOME").path_join(".g2a/hub.runtime.json")
	if not FileAccess.file_exists(path):
		return false
	var raw := FileAccess.get_file_as_string(path)
	var parsed = JSON.parse_string(raw)
	if typeof(parsed) == TYPE_DICTIONARY and parsed.has("origin_url"):
		hub_origin_url = str(parsed["origin_url"])
		return true
	return false

## Connects directly to Godot feature modules exposing `signal changed` and `snapshot() -> Dictionary`.
func bind_state_module(module_name: String, snapshot_fn: Callable, active_methods_fn: Callable, source_obj: Object = null) -> void:
	_signal_modules[module_name] = {
		"snapshot_fn": snapshot_fn,
		"active_methods_fn": active_methods_fn,
	}
	if source_obj != null and source_obj.has_signal("changed"):
		source_obj.connect("changed", func(): notify_module_changed(module_name, true))

func notify_module_changed(module_name: String, significant_event: bool = false) -> void:
	if _signal_modules.has(module_name):
		flush_dirty_sync(significant_event)

func attach_extension_data(extension_name: String, data: Dictionary, flush: bool = false) -> void:
	_extensions_data[extension_name] = data.duplicate(true)
	if flush:
		flush_dirty_sync(false)

func attach_visual_frame(visual_ref: Dictionary, flush: bool = false) -> void:
	_visual_ref = visual_ref.duplicate(true)
	if flush:
		flush_dirty_sync(false)

func attach_audio_segment(audio_ref: Dictionary, flush: bool = false) -> void:
	_audio_ref = audio_ref.duplicate(true)
	if flush:
		flush_dirty_sync(false)

func set_allowed_timeline_tags(tags: Array, flush: bool = false) -> void:
	_allowed_timeline_tags = tags.duplicate()
	if flush:
		flush_dirty_sync(true)

func publish_capabilities_and_baseline(origin_execute_url: String = "") -> void:
	var ids := _capabilities.keys()
	ids.sort()
	var defs: Array = []
	for mid in ids:
		defs.append(_capabilities[mid]["def"])
	var cap_digest := G2AJcs.compute_sha256("g2a.cap.v1", defs)
	var cap_payload := {
		"slot_id": slot_id,
		"role_kind": slot_role_kind,
		"billing_mode": billing_mode,
		"origin_execute_url": origin_execute_url,
		"capability_table_digest": cap_digest,
		"capabilities": defs,
	}
	if slot_disabled:
		cap_payload["disabled"] = true
	if max_sessions > 0:
		cap_payload["max_sessions"] = max_sessions
	if shared_cooldown_ms > 0:
		cap_payload["shared_cooldown_ms"] = shared_cooldown_ms
	if not allowlisted_cores.is_empty():
		var cores_arr: Array = []
		for c in allowlisted_cores:
			cores_arr.append(c)
		cap_payload["allowlisted_cores"] = cores_arr
	if allowlist_criteria_url != "":
		cap_payload["allowlist_criteria_url"] = allowlist_criteria_url
	if room_id != "":
		cap_payload["room_id"] = room_id
	if faction_id != "":
		cap_payload["faction_id"] = faction_id
	_post_to_hub("origin.capabilities", cap_payload)
	flush_full_sync()

func advance_epoch() -> void:
	current_epoch += 1
	current_rev = 0
	_last_sent_methods.clear()
	for exec_id in _in_flight_steps.keys():
		cancel_in_flight_step(exec_id)
	flush_full_sync()

func report_player_command(command_text: String) -> void:
	flush_dirty_sync(true)
	_post_to_hub("origin.player_event", {
		"slot_id": slot_id,
		"kind": "player_command",
		"text": command_text,
	})

func report_player_presence(online: bool, remaining_online_humans: int = 0) -> void:
	sync_enabled = online or remaining_online_humans > 0
	_post_to_hub("origin.player_event", {
		"slot_id": slot_id,
		"kind": "presence_changed",
		"online": online,
		"remaining_online_humans": remaining_online_humans,
	})

func report_emergency_stop() -> void:
	for exec_id in _in_flight_steps.keys():
		cancel_in_flight_step(exec_id)
	_post_to_hub("origin.player_event", {
		"slot_id": slot_id,
		"kind": "emergency_stop",
	})

func report_queue_approval(session_id: String, queue_id: String, queue_digest: String, fingerprint: String, approved: bool) -> void:
	_post_to_hub("origin.player_event", {
		"slot_id": slot_id,
		"kind": "queue_approval",
		"session_id": session_id,
		"queue_id": queue_id,
		"queue_digest": queue_digest,
		"fingerprint": fingerprint,
		"approved": approved,
	})

func report_npc_host_handoff(new_player_id: String) -> void:
	if slot_role_kind != "independent_npc" or billing_mode != "player_metered":
		push_error("G2A: report_npc_host_handoff requires player_metered independent_npc slot")
		return
	var cleaned := new_player_id.strip_edges()
	if cleaned == "":
		return
	for exec_id in _in_flight_steps.keys():
		cancel_in_flight_step(exec_id)
	sync_enabled = true
	_post_to_hub("origin.player_event", {
		"slot_id": slot_id,
		"kind": "host_handoff",
		"new_player_id": cleaned,
	})

func flush_dirty_sync(significant_event: bool = false) -> bool:
	if not sync_enabled and _pending_settled.is_empty():
		return false
	var view := _collect_current_view()
	var brief: Dictionary = view["brief"]
	var grants: Dictionary = view["grants"]
	var settled := _pending_settled.duplicate(true)
	_pending_settled.clear()

	var has_extra := (not _extensions_data.is_empty()) or _visual_ref != null or _audio_ref != null or _allowed_timeline_tags != null
	var digest_doc: Variant = {
		"brief": brief,
		"ext": _extensions_data,
		"visual": _visual_ref,
		"audio": _audio_ref,
		"timelines": _allowed_timeline_tags,
	} if has_extra else brief
	var brief_digest := G2AJcs.compute_sha256("g2a.brief.v1", digest_doc)
	var upserted: Array = []
	var removed: Array = []
	var current_serialized: Dictionary = {}

	for k in grants.keys():
		var canon := G2AJcs.canonicalize(grants[k])
		current_serialized[k] = canon
		if not _last_sent_methods.has(k) or _last_sent_methods[k] != canon:
			upserted.append(grants[k])
	for old_k in _last_sent_methods.keys():
		if not current_serialized.has(old_k):
			var parts := str(old_k).split("|", true, 1)
			removed.append({"method_id": parts[0], "actor": parts[1]})

	if brief_digest == _last_sent_brief_digest and upserted.is_empty() and removed.is_empty() and settled.is_empty():
		zero_change_skips += 1
		return false

	var prev_rev := current_rev
	current_rev += 1
	frames_synced += 1
	_last_sent_brief_digest = brief_digest
	_last_sent_methods = current_serialized
	var delta_frame := {
		"slot_id": slot_id,
		"epoch": current_epoch,
		"rev": current_rev,
		"base_rev": prev_rev,
		"mode": "delta",
		"significant_event": significant_event,
		"state_brief": brief,
		"upserted_methods": upserted,
		"removed_methods": removed,
		"settled_executions": settled,
	}
	if _visual_ref != null:
		delta_frame["visual_ref"] = _visual_ref.duplicate(true)
	if _audio_ref != null:
		delta_frame["audio_ref"] = _audio_ref.duplicate(true)
	if _allowed_timeline_tags != null:
		delta_frame["allowed_timeline_tags"] = _allowed_timeline_tags.duplicate()
	if not _extensions_data.is_empty():
		delta_frame["extensions_data"] = _extensions_data.duplicate(true)
	_post_to_hub("origin.sync", delta_frame)
	return true

func flush_full_sync() -> void:
	var view := _collect_current_view()
	var brief: Dictionary = view["brief"]
	var grants: Dictionary = view["grants"]
	var settled := _pending_settled.duplicate(true)
	_pending_settled.clear()

	current_rev += 1
	frames_synced += 1
	var has_extra := (not _extensions_data.is_empty()) or _visual_ref != null or _audio_ref != null or _allowed_timeline_tags != null
	var digest_doc: Variant = {
		"brief": brief,
		"ext": _extensions_data,
		"visual": _visual_ref,
		"audio": _audio_ref,
		"timelines": _allowed_timeline_tags,
	} if has_extra else brief
	_last_sent_brief_digest = G2AJcs.compute_sha256("g2a.brief.v1", digest_doc)
	_last_sent_methods.clear()
	for k in grants.keys():
		_last_sent_methods[k] = G2AJcs.canonicalize(grants[k])

	var full_frame := {
		"slot_id": slot_id,
		"epoch": current_epoch,
		"rev": current_rev,
		"mode": "full",
		"significant_event": true,
		"state_brief": brief,
		"upserted_methods": grants.values(),
		"removed_methods": [],
		"settled_executions": settled,
	}
	if _visual_ref != null:
		full_frame["visual_ref"] = _visual_ref.duplicate(true)
	if _audio_ref != null:
		full_frame["audio_ref"] = _audio_ref.duplicate(true)
	if _allowed_timeline_tags != null:
		full_frame["allowed_timeline_tags"] = _allowed_timeline_tags.duplicate()
	if not _extensions_data.is_empty():
		full_frame["extensions_data"] = _extensions_data.duplicate(true)
	_post_to_hub("origin.sync", full_frame)

func _store_ledger_reply(rep: Dictionary) -> void:
	var eid: String = rep["execution_id"]
	if not _execution_ledger.has(eid):
		_ledger_order.append(eid)
	_execution_ledger[eid] = rep
	var guard := _ledger_order.size()
	while _execution_ledger.size() > MAX_EXECUTION_LEDGER and guard > 0:
		guard -= 1
		var oldest := _ledger_order.pop_front()
		if _in_flight_steps.has(oldest):
			_ledger_order.append(oldest)
		else:
			_execution_ledger.erase(oldest)

## Handles `origin.execute` (`invoke` / `cancel` / `deliver_chat`) directly on the game thread.
func handle_origin_execute(req: Dictionary) -> Dictionary:
	var exec_id: String = req["execution_id"]
	var kind: String = req.get("kind", "invoke")
	if kind == "cancel":
		return cancel_in_flight_step(exec_id)
	if kind == "deliver_chat":
		var args: Dictionary = req.get("args", {})
		emit_signal("ai_chat_delivered", str(args.get("audience", "player_only")), str(args.get("text", "")))
		return {"execution_id": exec_id, "state": "succeeded"}

	if _execution_ledger.has(exec_id):
		return _execution_ledger[exec_id]
	if int(req.get("epoch", 0)) != current_epoch:
		return {"execution_id": exec_id, "state": "failed", "failure_reason": "stale_epoch"}

	var method_id: String = req.get("method_id", "")
	var actor: String = req.get("actor", "")
	var reg: Dictionary = _capabilities.get(method_id, {})
	if reg.is_empty() or not _actors.has(actor):
		return {"execution_id": exec_id, "state": "failed", "failure_reason": "invalid_method_or_actor"}
	var permitted: Array = reg["def"].get("permitted_actors", [])
	if not permitted.has(actor):
		return {"execution_id": exec_id, "state": "failed", "failure_reason": "permission_denied"}
	var step_args: Dictionary = req.get("args", {})
	var schema: Dictionary = reg["def"].get("params_schema", {})
	for req_key in schema.get("required", []):
		if not step_args.has(req_key):
			return {"execution_id": exec_id, "state": "failed", "failure_reason": "missing_required_arg:" + str(req_key)}

	var outcome: Dictionary = reg["effect_handler"].call(actor, _actors[actor], step_args, exec_id)
	var state: String = outcome.get("state", "succeeded")
	if state == "executing":
		_in_flight_steps[exec_id] = outcome.get("cancel_fn", Callable())
		var exec_rep := {"execution_id": exec_id, "state": "executing"}
		_store_ledger_reply(exec_rep)
		return exec_rep

	var rep := {
		"execution_id": exec_id,
		"state": state,
	}
	if outcome.has("effect_summary"):
		rep["effect_summary"] = outcome["effect_summary"]
	if outcome.has("failure_reason"):
		rep["failure_reason"] = outcome["failure_reason"]
	_store_ledger_reply(rep)
	call_deferred("flush_dirty_sync", false)
	return rep

func complete_step(execution_id: String, succeeded: bool, effect_summary: Dictionary = {}, failure_reason: String = "") -> void:
	complete_steps_batch([{
		"execution_id": execution_id,
		"succeeded": succeeded,
		"effect_summary": effect_summary,
		"failure_reason": failure_reason,
	}])

## Atomically settles multiple sustained steps and coalesces them into a single `origin.sync` delta frame.
func complete_steps_batch(items: Array) -> int:
	var settled_count := 0
	for item in items:
		var eid := str(item.get("execution_id", ""))
		if not _in_flight_steps.has(eid):
			continue
		_in_flight_steps.erase(eid)
		var ok := bool(item.get("succeeded", true))
		var rep: Dictionary = {
			"execution_id": eid,
			"state": "succeeded" if ok else "failed",
		}
		var sum: Dictionary = item.get("effect_summary", {})
		var reason := str(item.get("failure_reason", ""))
		if not sum.is_empty():
			rep["effect_summary"] = sum
		if reason != "":
			rep["failure_reason"] = reason
		_store_ledger_reply(rep)
		_pending_settled.append(rep)
		settled_count += 1
	if settled_count > 0:
		flush_dirty_sync(false)
	return settled_count

func cancel_in_flight_step(execution_id: String) -> Dictionary:
	if _in_flight_steps.has(execution_id):
		var cancel_fn: Callable = _in_flight_steps[execution_id]
		_in_flight_steps.erase(execution_id)
		if cancel_fn.is_valid():
			cancel_fn.call()
		var rep := {"execution_id": execution_id, "state": "cancelled", "failure_reason": "cancelled_by_hub"}
		_store_ledger_reply(rep)
		_pending_settled.append(rep)
		flush_dirty_sync(false)
		return rep
	return _execution_ledger.get(execution_id, {"execution_id": execution_id, "state": "cancelled"})

static func _parse_anchor_meters(anchor: Variant) -> float:
	var s := str(anchor).strip_edges()
	if not s.ends_with("m"):
		return 1e9
	var core := s.substr(0, s.length() - 1)
	var sp := core.rfind(" ")
	if sp >= 0:
		core = core.substr(sp + 1)
	return float(core) if core.is_valid_float() else 1e9

static func _prioritize_targets(raw_targets: Array) -> Array:
	var dedup := {}
	for t in raw_targets:
		var tid := str(t.get("target_id", ""))
		if not dedup.has(tid) or str(t.get("readiness", "")) == "ready":
			dedup[tid] = t
	var ordered: Array = dedup.values()
	ordered.sort_custom(func(a: Dictionary, b: Dictionary) -> bool:
		var a_alert := 0 if str(a.get("alert_tag", "")) != "" else 1
		var b_alert := 0 if str(b.get("alert_tag", "")) != "" else 1
		if a_alert != b_alert:
			return a_alert < b_alert
		var a_pt := 0 if str(a.get("player_anchor", "")).to_lower().find("pointed") >= 0 else 1
		var b_pt := 0 if str(b.get("player_anchor", "")).to_lower().find("pointed") >= 0 else 1
		if a_pt != b_pt:
			return a_pt < b_pt
		var a_rdy := 0 if str(a.get("readiness", "")) == "ready" else 1
		var b_rdy := 0 if str(b.get("readiness", "")) == "ready" else 1
		if a_rdy != b_rdy:
			return a_rdy < b_rdy
		var d_diff := _parse_anchor_meters(a.get("actor_anchor", "")) - _parse_anchor_meters(b.get("actor_anchor", ""))
		if abs(d_diff) > 0.001:
			return d_diff < 0.0
		return str(a.get("target_id", "")) < str(b.get("target_id", ""))
	)
	return ordered.slice(0, MAX_CANDIDATE_TARGETS) if ordered.size() > MAX_CANDIDATE_TARGETS else ordered

static func _merge_grant(grants: Dictionary, incoming: Dictionary) -> void:
	var key := "%s|%s" % [incoming["method_id"], incoming["actor"]]
	var inc_targets: Array = incoming.get("targets", [])
	if grants.has(key):
		var prev: Dictionary = grants[key]
		var combined: Array = prev.get("targets", []) + inc_targets
		var merged_targets := _prioritize_targets(combined)
		var overall := "staged"
		if prev.get("readiness", "") == "ready" or incoming.get("readiness", "") == "ready":
			overall = "ready"
		else:
			for t in merged_targets:
				if str(t.get("readiness", "")) == "ready":
					overall = "ready"
					break
		grants[key] = {
			"method_id": incoming["method_id"],
			"actor": incoming["actor"],
			"readiness": overall,
			"targets": merged_targets,
		}
	else:
		grants[key] = {
			"method_id": incoming["method_id"],
			"actor": incoming["actor"],
			"readiness": incoming.get("readiness", "ready"),
			"targets": _prioritize_targets(inc_targets),
		}

func _collect_current_view() -> Dictionary:
	var brief := {}
	var grants := {}
	for mod_name in _signal_modules.keys():
		var mod: Dictionary = _signal_modules[mod_name]
		brief[mod_name] = mod["snapshot_fn"].call()
		for g in mod["active_methods_fn"].call():
			_merge_grant(grants, g)
	return {"brief": brief, "grants": grants}

func _post_to_hub(operation: String, payload: Dictionary) -> void:
	if not is_inside_tree():
		return
	_ensure_http_worker()
	var env := {
		"g2a_version": G2A_WIRE_VERSION,
		"request_id": "gd-%d-%d" % [Time.get_ticks_usec(), _outbound_queue.size()],
		"timestamp_ms": int(Time.get_unix_time_from_system() * 1000.0),
		"operation": operation,
		"payload": payload,
		"extensions": {},
	}
	_outbound_queue.append(env)
	_pump_outbound_queue()

func _pump_outbound_queue() -> void:
	if _http_busy or _http_worker == null or _outbound_queue.is_empty():
		return
	var next_env: Dictionary = _outbound_queue.pop_front()
	var headers := PackedStringArray(["Content-Type: application/json"])
	if origin_token != "":
		headers.append("Authorization: Bearer " + origin_token)
	_http_busy = true
	var err := _http_worker.request(
		hub_origin_url.trim_suffix("/") + "/g2a/v1/origin",
		headers,
		HTTPClient.METHOD_POST,
		JSON.stringify(next_env)
	)
	if err != OK:
		_http_busy = false
