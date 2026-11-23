@tool
extends EditorPlugin

func _enter_tree() -> void:
	add_custom_type("G2AOriginSDK", "Node", preload("res://addons/g2a_origin/g2a_origin_sdk.gd"), null)

func _exit_tree() -> void:
	remove_custom_type("G2AOriginSDK")
