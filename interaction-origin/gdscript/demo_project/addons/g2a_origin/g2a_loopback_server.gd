class_name G2ALoopbackServer
extends Node
## Embedded Godot 4.x loopback HTTP/1.1 Keep-Alive listener receiving Hub -> Origin `origin.execute` requests via TCPServer.
## Buffers accepted StreamPeerTCP connections across frames via raw byte framing and reuses persistent sockets across multi-step queues.

const MAX_HEADER_BYTES := 64 * 1024
const MAX_REQUEST_BYTES := 4 * 1024 * 1024
const PEER_IDLE_TIMEOUT_MS := 60000

@export var origin_sdk: G2AOriginSDK
@export var listen_port: int = 0
@export var shared_token: String = ""

var _server := TCPServer.new()
var _bound_port: int = 0
var _active_peers: Array[Dictionary] = []

func start_listener() -> String:
	var err := _server.listen(listen_port, "127.0.0.1")
	if err != OK:
		push_error("G2ALoopbackServer failed to listen on 127.0.0.1: %d" % err)
		return ""
	_bound_port = _server.get_local_port()
	return "http://127.0.0.1:%d/g2a/v1/origin/execute" % _bound_port

func stop_listener() -> void:
	for entry in _active_peers:
		var p: StreamPeerTCP = entry["peer"]
		p.disconnect_from_host()
	_active_peers.clear()
	if _server.is_listening():
		_server.stop()

func _process(_delta: float) -> void:
	if not _server.is_listening() or origin_sdk == null:
		return
	var now_ms := Time.get_ticks_msec()
	while _server.is_connection_available():
		var peer := _server.take_connection()
		if peer != null:
			_active_peers.append({"peer": peer, "buf": PackedByteArray(), "last_active_ms": now_ms})

	var idx := _active_peers.size() - 1
	while idx >= 0:
		var entry: Dictionary = _active_peers[idx]
		if _pump_peer(entry, now_ms):
			_active_peers.remove_at(idx)
		idx -= 1

func _find_header_end(buf: PackedByteArray) -> int:
	var limit := buf.size() - 4
	for i in range(limit + 1):
		if buf[i] == 0x0D and buf[i + 1] == 0x0A and buf[i + 2] == 0x0D and buf[i + 3] == 0x0A:
			return i
	return -1

func _pump_peer(entry: Dictionary, now_ms: int) -> bool:
	var peer: StreamPeerTCP = entry["peer"]
	peer.poll()
	var status := peer.get_status()
	if status == StreamPeerTCP.STATUS_NONE or status == StreamPeerTCP.STATUS_ERROR:
		return true
	if now_ms - int(entry["last_active_ms"]) > PEER_IDLE_TIMEOUT_MS:
		peer.disconnect_from_host()
		return true

	var avail := peer.get_available_bytes()
	if avail > 0:
		var chunk_res := peer.get_partial_data(avail)
		if chunk_res[0] == OK:
			var buf: PackedByteArray = entry["buf"]
			buf.append_array(chunk_res[1])
			if buf.size() > MAX_HEADER_BYTES + MAX_REQUEST_BYTES:
				peer.disconnect_from_host()
				return true
			entry["buf"] = buf
			entry["last_active_ms"] = now_ms

	while true:
		var cur_buf: PackedByteArray = entry["buf"]
		if cur_buf.size() < 4:
			return false
		var hdr_end := _find_header_end(cur_buf)
		if hdr_end < 0:
			if cur_buf.size() > MAX_HEADER_BYTES:
				peer.disconnect_from_host()
				return true
			return false

		var header_block := cur_buf.slice(0, hdr_end).get_string_from_ascii()
		var header_byte_len := hdr_end + 4
		var expected_len := _parse_content_length(header_block)
		if expected_len < 0 or expected_len > MAX_REQUEST_BYTES:
			_write_reply(peer, 413, "", "not_accepted", null, {"code": "invalid_request", "http_status": 413, "message": "Invalid or oversized Content-Length"}, true)
			return true
		if cur_buf.size() < header_byte_len + expected_len:
			return false

		var body_bytes := cur_buf.slice(header_byte_len, header_byte_len + expected_len)
		entry["buf"] = cur_buf.slice(header_byte_len + expected_len)
		entry["last_active_ms"] = now_ms
		var want_close := header_block.to_lower().find("connection: close") >= 0
		_dispatch_http_request(peer, header_block, body_bytes.get_string_from_utf8(), want_close)
		if want_close:
			return true

	return false

func _parse_content_length(header_block: String) -> int:
	for line in header_block.split("\r\n"):
		if line.to_lower().begins_with("content-length:"):
			return int(line.substr(15).strip_edges())
	return 0

func _dispatch_http_request(peer: StreamPeerTCP, header_block: String, body_text: String, close_conn: bool) -> void:
	if shared_token != "" and header_block.find("Bearer " + shared_token) < 0:
		_write_reply(peer, 401, "", "not_accepted", null, {"code": "unauthenticated", "http_status": 401, "message": "Invalid Origin token"}, close_conn)
		return

	var parsed = JSON.parse_string(body_text)
	if typeof(parsed) != TYPE_DICTIONARY or parsed.get("operation", "") != "origin.execute":
		_write_reply(peer, 400, "", "not_accepted", null, {"code": "invalid_request", "http_status": 400, "message": "Expected origin.execute"}, close_conn)
		return

	var req_id: String = str(parsed.get("request_id", ""))
	var exec_reply := origin_sdk.handle_origin_execute(parsed.get("payload", {}))
	_write_reply(peer, 200, req_id, "accepted", exec_reply, null, close_conn)

func _write_reply(peer: StreamPeerTCP, status: int, req_id: String, acceptance: String, result: Variant, err_obj: Variant, close_conn: bool) -> void:
	var env := {
		"g2a_version": G2AOriginSDK.G2A_WIRE_VERSION,
		"request_id": req_id,
		"timestamp_ms": int(Time.get_unix_time_from_system() * 1000.0),
		"acceptance": acceptance,
		"result": result,
		"error": err_obj,
	}
	var body_bytes := JSON.stringify(env).to_utf8_buffer()
	var reason := "OK" if status == 200 else "Error"
	var conn_hdr := "close" if close_conn else "keep-alive"
	var head := "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: %s\r\n\r\n" % [status, reason, body_bytes.size(), conn_hdr]
	peer.put_data(head.to_ascii_buffer())
	peer.put_data(body_bytes)
	if close_conn:
		peer.disconnect_from_host()
