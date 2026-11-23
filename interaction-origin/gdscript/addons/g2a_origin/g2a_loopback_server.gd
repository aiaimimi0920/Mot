class_name G2ALoopbackServer
extends Node
## Embedded Godot 4.x loopback HTTP/1.1 listener receiving Hub -> Origin `origin.execute` requests via TCPServer.

@export var origin_sdk: G2AOriginSDK
@export var listen_port: int = 0
@export var shared_token: String = ""

var _server := TCPServer.new()
var _bound_port: int = 0

func start_listener() -> String:
	var err := _server.listen(listen_port, "127.0.0.1")
	if err != OK:
		push_error("G2ALoopbackServer failed to listen on 127.0.0.1: %d" % err)
		return ""
	_bound_port = _server.get_local_port()
	return "http://127.0.0.1:%d/g2a/v1/origin/execute" % _bound_port

func stop_listener() -> void:
	if _server.is_listening():
		_server.stop()

func _process(_delta: float) -> void:
	if not _server.is_listening() or origin_sdk == null:
		return
	while _server.is_connection_available():
		var peer := _server.take_connection()
		if peer != null:
			_handle_peer(peer)

func _handle_peer(peer: StreamPeerTCP) -> void:
	peer.poll()
	var avail := peer.get_available_bytes()
	if avail <= 0:
		return
	var raw := peer.get_utf8_string(avail)
	var split_idx := raw.find("\r\n\r\n")
	if split_idx < 0:
		peer.disconnect_from_host()
		return
	var header_block := raw.substr(0, split_idx)
	var body_text := raw.substr(split_idx + 4)

	if shared_token != "" and header_block.find("Bearer " + shared_token) < 0:
		_write_reply(peer, 401, "", "not_accepted", null, {"code": "unauthenticated", "http_status": 401, "message": "Invalid Origin token"})
		return

	var parsed = JSON.parse_string(body_text)
	if typeof(parsed) != TYPE_DICTIONARY or parsed.get("operation", "") != "origin.execute":
		_write_reply(peer, 400, "", "not_accepted", null, {"code": "invalid_request", "http_status": 400, "message": "Expected origin.execute"})
		return

	var req_id: String = str(parsed.get("request_id", ""))
	var exec_reply := origin_sdk.handle_origin_execute(parsed.get("payload", {}))
	_write_reply(peer, 200, req_id, "accepted", exec_reply, null)

func _write_reply(peer: StreamPeerTCP, status: int, req_id: String, acceptance: String, result: Variant, err_obj: Variant) -> void:
	var env := {
		"g2a_version": G2AOriginSDK.G2A_WIRE_VERSION,
		"request_id": req_id,
		"timestamp_ms": int(Time.get_unix_time_from_system() * 1000.0),
		"acceptance": acceptance,
		"result": result,
		"error": err_obj,
	}
	var body_bytes := JSON.stringify(env).to_utf8_buffer()
	var head := "HTTP/1.1 %d OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % [status, body_bytes.size()]
	peer.put_data(head.to_ascii_buffer())
	peer.put_data(body_bytes)
	peer.disconnect_from_host()
