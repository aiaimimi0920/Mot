from __future__ import annotations

import hashlib
import json
import math
from typing import Any, List, Tuple


class StrictJSONError(ValueError):
    pass


def _reject_constant(val: str) -> Any:
    raise StrictJSONError(f"Non-finite JSON constant {val!r} is forbidden")


def _no_duplicate_pairs(pairs: List[Tuple[str, Any]]) -> dict:
    obj: dict = {}
    for k, v in pairs:
        if k in obj:
            raise StrictJSONError(f"Duplicate JSON object key {k!r}")
        obj[k] = v
    return obj


def parse_strict_json(raw: str | bytes) -> Any:
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    return json.loads(text, object_pairs_hook=_no_duplicate_pairs, parse_constant=_reject_constant)


def _validate_value(val: Any) -> None:
    if val is None or isinstance(val, (bool, str, int)):
        return
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise StrictJSONError("Non-finite float is forbidden")
        return
    if isinstance(val, dict):
        for k, child in val.items():
            if not isinstance(k, str):
                raise StrictJSONError("JSON object keys must be strings")
            _validate_value(child)
        return
    if isinstance(val, (list, tuple)):
        for item in val:
            _validate_value(item)
        return
    raise StrictJSONError(f"Unsupported JSON value type: {type(val).__name__}")


def _escape_jcs_str(s: str) -> str:
    out = ['"']
    for ch in s:
        c = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif c <= 0x1F:
            out.append(f"\\u{c:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _format_jcs_float(f: float) -> str:
    if math.isnan(f) or math.isinf(f):
        raise StrictJSONError("Non-finite float is forbidden")
    if f == 0.0:
        return "0"
    abs_f = abs(f)
    if 1e-6 <= abs_f < 1e21:
        if f.is_integer() and abs_f <= 9007199254740991:
            return str(int(f))
        s = repr(f)
        if "e" not in s and "E" not in s:
            return s.rstrip("0").rstrip(".") if "." in s else s
        return f"{f:.16f}".rstrip("0").rstrip(".")
    s = repr(f).lower()
    if "e" in s:
        mant, exp_str = s.split("e", 1)
        if "." in mant:
            mant = mant.rstrip("0").rstrip(".")
        exp_val = int(exp_str)
        sign = "+" if exp_val >= 0 else ""
        return f"{mant}e{sign}{exp_val}"
    return s


def _serialize_jcs(val: Any) -> str:
    if val is None:
        return "null"
    if val is True:
        return "true"
    if val is False:
        return "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return _format_jcs_float(val)
    if isinstance(val, str):
        return _escape_jcs_str(val)
    if isinstance(val, (list, tuple)):
        return "[" + ",".join(_serialize_jcs(v) for v in val) + "]"
    if isinstance(val, dict):
        raw_keys = val.keys()
        keys = sorted(raw_keys) if all(k.isascii() for k in raw_keys) else sorted(raw_keys, key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(f"{_escape_jcs_str(k)}:{_serialize_jcs(val[k])}" for k in keys) + "}"
    raise StrictJSONError(f"Cannot canonicalize {type(val).__name__}")


def canonicalize_utf8(val: Any) -> bytes:
    _validate_value(val)
    return _serialize_jcs(val).encode("utf-8")


def jcs_sha256(domain_tag: str, val: Any) -> str:
    canon = canonicalize_utf8(val)
    h = hashlib.sha256()
    h.update(domain_tag.encode("utf-8"))
    h.update(b"\x00")
    h.update(canon)
    return "sha256:" + h.hexdigest()
