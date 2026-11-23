extends Node
## Example Godot 4.x controller wiring signal-snapshot modules (wallet / crafting) to G2AOriginSDK + G2ALoopbackServer.

@onready var origin_sdk := G2AOriginSDK.new()
@onready var loopback_srv := G2ALoopbackServer.new()

var gold: int = 300
var herbs: int = 0
var potion_ready: bool = false

func _ready() -> void:
	add_child(origin_sdk)
	loopback_srv.origin_sdk = origin_sdk
	add_child(loopback_srv)

	origin_sdk.bind_actor_subject("companion", self)
	origin_sdk.bind_actor_subject("player", self)
	origin_sdk.bind_state_module("nightbar", _snapshot_bar, _active_bar_methods)

	origin_sdk.register_capability({
		"method_id": "#05",
		"name": "trade_resource",
		"description": "Purchase crafting ingredients",
		"permitted_actors": ["player"],
		"params_schema": {"type": "object", "required": ["item", "qty"]},
		"sustained": false,
		"risk_flags": ["economy_spend"],
		"default_policy": "allow",
	}, _effect_trade_resource)

	origin_sdk.register_capability({
		"method_id": "#06",
		"name": "craft",
		"description": "Brew nightbar recipe",
		"permitted_actors": ["companion", "player"],
		"params_schema": {"type": "object", "required": ["recipe_id"]},
		"sustained": false,
		"risk_flags": [],
		"default_policy": "allow",
	}, _effect_craft)

	var exec_url := loopback_srv.start_listener()
	origin_sdk.publish_capabilities_and_baseline(exec_url)

func _snapshot_bar() -> Dictionary:
	return {"gold": gold, "herbs": herbs, "potion_ready": potion_ready}

func _active_bar_methods() -> Array:
	return [
		{
			"method_id": "#05",
			"actor": "player",
			"readiness": "ready" if gold >= 20 else "staged",
			"targets": [],
		},
		{
			"method_id": "#06",
			"actor": "companion",
			"readiness": "ready" if herbs >= 2 else "staged",
			"targets": [{
				"target_id": "moon_tea",
				"label": "Moonlit Herbal Tea",
				"readiness": "ready" if herbs >= 2 else "staged",
				"prerequisite_hint": "requires_resource:herbs" if herbs < 2 else "",
			}],
		},
	]

func _effect_trade_resource(_actor: String, _subj: Variant, args: Dictionary, _exec_id: String) -> Dictionary:
	var qty := int(args.get("qty", 1))
	if gold < qty * 10:
		return {"state": "failed", "failure_reason": "insufficient_gold"}
	gold -= qty * 10
	herbs += qty
	return {"state": "succeeded", "effect_summary": {"herbs": herbs, "gold": gold}}

func _effect_craft(_actor: String, _subj: Variant, _args: Dictionary, _exec_id: String) -> Dictionary:
	if herbs < 2:
		return {"state": "failed", "failure_reason": "insufficient_herbs"}
	herbs -= 2
	potion_ready = true
	return {"state": "succeeded", "effect_summary": {"crafted": "moon_tea"}}
