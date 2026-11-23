class_name G2ASpatialAdapter3D
extends Node3D
## Godot 4.x 3D & XR Spatial Adapter scanning `"g2a_interactable"` nodes while redacting `"g2a_hidden_secret"`.

@export var player_node: Node3D
@export var companion_node: Node3D
@export var pointing_ray: RayCast3D

func build_spatial_grants(method_id: String, actor: String, perception_radius: float, is_navigation_verb: bool = false) -> Dictionary:
	var actor_node := companion_node if actor == "companion" else player_node
	if actor_node == null or not is_inside_tree():
		return {}

	var pointed_collider: Object = pointing_ray.get_collider() if (pointing_ray != null and pointing_ray.is_colliding()) else null
	var candidates: Array = []

	for node in get_tree().get_nodes_in_group("g2a_interactable"):
		if not (node is Node3D):
			continue
		# Confidentiality redaction: unrevealed secret objects MUST NEVER leave Interaction Origin!
		if node.is_in_group("g2a_hidden_secret"):
			continue
		var target_3d := node as Node3D
		var dist := actor_node.global_position.distance_to(target_3d.global_position)
		if dist > perception_radius:
			continue
		var interact_range: float = float(target_3d.get_meta("interact_range", 2.0))
		var readiness := "ready" if (is_navigation_verb or dist <= interact_range) else "staged"
		var player_anchor := _compute_player_anchor(target_3d, pointed_collider == target_3d)
		var cand := {
			"target_id": str(target_3d.get_meta("g2a_target_id", target_3d.name)),
			"label": str(target_3d.get_meta("display_name", target_3d.name)),
			"readiness": readiness,
			"actor_anchor": "%.1fm" % dist,
			"player_anchor": player_anchor,
		}
		if readiness == "staged":
			cand["prerequisite_hint"] = "requires_proximity"
		candidates.append(cand)

	if candidates.is_empty():
		return {}
	var overall := "staged"
	for c in candidates:
		if c["readiness"] == "ready":
			overall = "ready"
			break
	return {
		"method_id": method_id,
		"actor": actor,
		"readiness": overall,
		"targets": candidates.slice(0, G2AOriginSDK.MAX_CANDIDATE_TARGETS),
	}

func _compute_player_anchor(target: Node3D, is_pointed: bool) -> String:
	if is_pointed:
		return "pointed_ray"
	if player_node == null:
		return "unknown"
	var local_vec := player_node.to_local(target.global_position)
	var dist := player_node.global_position.distance_to(target.global_position)
	if abs(local_vec.x) >= abs(local_vec.z):
		return "%s %.1fm" % ["right" if local_vec.x >= 0.0 else "left", dist]
	return "%s %.1fm" % ["ahead" if local_vec.z <= 0.0 else "behind", dist]
