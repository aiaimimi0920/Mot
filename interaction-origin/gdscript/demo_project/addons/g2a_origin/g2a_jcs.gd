class_name G2AJcs
extends RefCounted
## RFC 8785 (JCS) Canonical JSON & Domain-Separated SHA-256 Utility for Godot 4.x (G2A V0.0.1).

static func compute_sha256(domain_tag: String, value: Variant) -> String:
	var canon_utf8 := canonicalize(value).to_utf8_buffer()
	var ctx := HashingContext.new()
	ctx.start(HashingContext.HASH_SHA256)
	ctx.update(domain_tag.to_utf8_buffer())
	ctx.update(PackedByteArray([0]))
	ctx.update(canon_utf8)
	return "sha256:" + ctx.finish().hex_encode()

static func canonicalize(val: Variant) -> String:
	match typeof(val):
		TYPE_NIL:
			return "null"
		TYPE_BOOL:
			return "true" if val else "false"
		TYPE_INT:
			return str(val)
		TYPE_FLOAT:
			var f: float = val
			assert(not is_nan(f) and not is_inf(f), "G2A strict JSON forbids NaN/Infinity")
			if f == 0.0:
				return "0"
			if is_equal_approx(f, trunc(f)) and abs(f) <= 9007199254740991.0:
				return str(int(f))
			return JSON.stringify(f)
		TYPE_STRING, TYPE_STRING_NAME:
			return _escape_string(str(val))
		TYPE_ARRAY:
			var parts: PackedStringArray = []
			for item in val:
				parts.append(canonicalize(item))
			return "[" + ",".join(parts) + "]"
		TYPE_DICTIONARY:
			var keys: Array = val.keys()
			keys.sort_custom(_less_utf16)
			var kv_parts: PackedStringArray = []
			for k in keys:
				kv_parts.append(_escape_string(str(k)) + ":" + canonicalize(val[k]))
			return "{" + ",".join(kv_parts) + "}"
		_:
			push_error("Unsupported Variant in G2AJcs: %s" % str(val))
			return "null"

static func _less_utf16(a: Variant, b: Variant) -> bool:
	var sa := str(a)
	var sb := str(b)
	var ua := _to_utf16_units(sa)
	var ub := _to_utf16_units(sb)
	var n := mini(ua.size(), ub.size())
	for i in range(n):
		if ua[i] != ub[i]:
			return ua[i] < ub[i]
	return ua.size() < ub.size()

static func _to_utf16_units(s: String) -> PackedInt32Array:
	var out := PackedInt32Array()
	for i in range(s.length()):
		var cp := s.unicode_at(i)
		if cp <= 0xFFFF:
			out.append(cp)
		else:
			var adj := cp - 0x10000
			out.append(0xD800 + (adj >> 10))
			out.append(0xDC00 + (adj & 0x3FF))
	return out

static func _escape_string(s: String) -> String:
	var out := "\""
	for i in range(s.length()):
		var code := s.unicode_at(i)
		match code:
			0x22: out += "\\\""
			0x5C: out += "\\\\"
			0x08: out += "\\b"
			0x0C: out += "\\f"
			0x0A: out += "\\n"
			0x0D: out += "\\r"
			0x09: out += "\\t"
			_:
				if code <= 0x1F:
					out += "\\u%04x" % code
				else:
					out += s[i]
	out += "\""
	return out
