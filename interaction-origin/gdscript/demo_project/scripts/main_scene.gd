extends Control
## Complete Playable Godot 4.x G2A V0.0.1 Companion Demo Scene.
## Auto-discovers local Intelligent Hub, starts G2ALoopbackServer, and lets the player trigger
## both Embodied Chest Commands and Signal-Driven Workshop Alerts right from the HUD.

var origin_sdk := G2AOriginSDK.new()
var loopback_srv := G2ALoopbackServer.new()

var at_chest: bool = false
var chest_opened: bool = false
var gold: int = 500
var timber: int = 0
var bench_running: bool = true

var _status_label: Label
var _log_box: RichTextLabel

func _ready() -> void:
	_build_ui()
	add_child(origin_sdk)
	loopback_srv.origin_sdk = origin_sdk
	add_child(loopback_srv)

	origin_sdk.slot_id = "slot-godot-demo"
	origin_sdk.ai_chat_delivered.connect(_on_ai_chat)
	var found := origin_sdk.auto_discover_local_hub()

	origin_sdk.bind_actor_subject("companion", self)
	origin_sdk.bind_actor_subject("player", self)
	origin_sdk.bind_state_module("godot_world", _snapshot_world, _active_methods)

	origin_sdk.register_capability({
		"method_id": "#01",
		"name": "move_to",
		"description": "Walk companion to target",
		"permitted_actors": ["companion"],
		"params_schema": {"type": "object", "required": ["target_id"]},
		"sustained": true,
		"risk_flags": [],
		"default_policy": "allow",
	}, _effect_move_to)

	origin_sdk.register_capability({
		"method_id": "#02",
		"name": "interact",
		"description": "Open chest directly without hotkey lock",
		"permitted_actors": ["companion"],
		"params_schema": {"type": "object", "required": ["target_id"]},
		"sustained": false,
		"risk_flags": [],
		"default_policy": "allow",
	}, _effect_interact)

	origin_sdk.register_capability({
		"method_id": "#05",
		"name": "trade_resource",
		"description": "Buy timber for workshop",
		"permitted_actors": ["player"],
		"params_schema": {"type": "object", "required": ["item", "qty"]},
		"sustained": false,
		"risk_flags": ["economy_spend"],
		"default_policy": "allow",
	}, _effect_trade)

	origin_sdk.register_capability({
		"method_id": "#06",
		"name": "resume_line",
		"description": "Resume halted carpentry bench",
		"permitted_actors": ["player"],
		"params_schema": {"type": "object", "required": ["line_id"]},
		"sustained": false,
		"risk_flags": [],
		"default_policy": "allow",
	}, _effect_resume_line)

	var exec_url := loopback_srv.start_listener()
	origin_sdk.publish_capabilities_and_baseline(exec_url)
	_append_log("[Origin] Ready! Hub=%s (auto_discovered=%s) | Execute=%s" % [origin_sdk.hub_origin_url, str(found), exec_url])
	_refresh_status()

func _snapshot_world() -> Dictionary:
	return {
		"at_chest": at_chest,
		"chest_opened": chest_opened,
		"gold": gold,
		"timber": timber,
		"bench_running": bench_running,
	}

func _active_methods() -> Array:
	var grants: Array = []
	if not chest_opened:
		grants.append({
			"method_id": "#01",
			"actor": "companion",
			"readiness": "ready",
			"targets": [{"target_id": "chest_R", "label": "Right Wooden Chest", "readiness": "ready", "player_anchor": "right 4.0m"}],
		})
		grants.append({
			"method_id": "#02",
			"actor": "companion",
			"readiness": "ready" if at_chest else "staged",
			"targets": [{
				"target_id": "chest_R",
				"label": "Right Wooden Chest",
				"readiness": "ready" if at_chest else "staged",
				"player_anchor": "right 4.0m",
				"prerequisite_hint": "" if at_chest else "requires_proximity",
			}],
		})
	grants.append({"method_id": "#05", "actor": "player", "readiness": "ready" if gold >= 40 else "staged", "targets": []})
	if not bench_running:
		grants.append({
			"method_id": "#06",
			"actor": "player",
			"readiness": "ready" if timber >= 20 else "staged",
			"targets": [{
				"target_id": "Bench_1",
				"label": "Carpentry Bench 1",
				"readiness": "ready" if timber >= 20 else "staged",
				"alert_tag": "halted_no_material" if timber < 20 else "",
			}],
		})
	return grants

func _effect_move_to(_actor: String, _subj: Variant, _args: Dictionary, exec_id: String) -> Dictionary:
	_append_log("[Origin] #01 move_to executing (0.4s simulated pathfinding)...")
	var timer := get_tree().create_timer(0.4)
	timer.timeout.connect(func():
		at_chest = true
		origin_sdk.complete_step(exec_id, true, {"arrived": "chest_R"})
		_append_log("[Origin] #01 arrived at chest_R -> synced settled_executions + #02 ready!")
		_refresh_status()
	)
	return {"state": "executing", "cancel_fn": func(): _append_log("[Origin] #01 move_to cancelled!")}

func _effect_interact(_actor: String, _subj: Variant, _args: Dictionary, _exec_id: String) -> Dictionary:
	chest_opened = true
	_append_log("[Origin] #02 interact succeeded -> Right Chest opened (gold +50)!")
	gold += 50
	_refresh_status()
	return {"state": "succeeded", "effect_summary": {"loot": ["gold_50"]}}

func _effect_trade(_actor: String, _subj: Variant, args: Dictionary, _exec_id: String) -> Dictionary:
	var qty := int(args.get("qty", 20))
	gold -= qty * 2
	timber += qty
	_append_log("[Origin] #05 trade_resource bought %d timber!" % qty)
	_refresh_status()
	return {"state": "succeeded", "effect_summary": {"timber": timber, "gold": gold}}

func _effect_resume_line(_actor: String, _subj: Variant, _args: Dictionary, _exec_id: String) -> Dictionary:
	timber -= 20
	bench_running = true
	_append_log("[Origin] #06 resume_line -> Bench_1 resumed production!")
	_refresh_status()
	return {"state": "succeeded", "effect_summary": {"bench_running": true}}

func _on_ai_chat(audience: String, text: String) -> void:
	_append_log("[AI Companion -> %s] %s" % [audience, text])

func _build_ui() -> void:
	var vbox := VBoxContainer.new()
	vbox.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT, Control.PRESET_MODE_MINSIZE, 20)
	vbox.add_theme_constant_override("separation", 12)
	add_child(vbox)

	var title := Label.new()
	title.text = "G2A V0.0.1 Godot 4.x Companion Playground (slot-godot-demo)"
	vbox.add_child(title)

	_status_label = Label.new()
	vbox.add_child(_status_label)

	var hbox := HBoxContainer.new()
	hbox.add_theme_constant_override("separation", 10)
	vbox.add_child(hbox)

	var btn_chest := Button.new()
	btn_chest.text = "1. Speak Command: Open Right Chest"
	btn_chest.pressed.connect(func():
		_append_log("[Player] Command -> '打开右侧的宝箱'")
		origin_sdk.report_player_command("打开右侧的宝箱")
	)
	hbox.add_child(btn_chest)

	var btn_alert := Button.new()
	btn_alert.text = "2. Trigger Bench Timber Exhaustion Alert"
	btn_alert.pressed.connect(func():
		bench_running = false
		timber = 0
		_refresh_status()
		_append_log("[Game Signal] Bench_1 halted_no_material -> notify_module_changed(true)")
		origin_sdk.notify_module_changed("godot_world", true)
	)
	hbox.add_child(btn_alert)

	var btn_stop := Button.new()
	btn_stop.text = "Emergency Stop"
	btn_stop.pressed.connect(func():
		origin_sdk.report_emergency_stop()
		_append_log("[Player] Emergency Stop sent!")
	)
	hbox.add_child(btn_stop)

	_log_box = RichTextLabel.new()
	_log_box.size_flags_vertical = Control.SIZE_EXPAND_FILL
	vbox.add_child(_log_box)

func _refresh_status() -> void:
	if _status_label != null:
		_status_label.text = "Gold: %d | Timber: %d | Bench Running: %s | At Chest: %s | Chest Opened: %s" % [
			gold, timber, str(bench_running), str(at_chest), str(chest_opened)
		]

func _append_log(line: String) -> void:
	if _log_box != null:
		_log_box.append_text(line + "\n")
