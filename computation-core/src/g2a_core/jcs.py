from __future__ import annotations

import hashlib
import json
from json.encoder import encode_basestring as _c_encode_jcs_string
import math
from typing import Any, List, Tuple

from .schemas import G2AError


def _reject_constant(val: str) -> Any:
    raise G2AError("invalid_request", 400, f"Non-finite JSON constant {val!r} is forbidden")


def _no_duplicate_object_pairs(pairs: List[Tuple[str, Any]]) -> dict:
    obj: dict = {}
    for k, v in pairs:
        if k in obj:
            raise G2AError("invalid_request", 400, f"Duplicate JSON object key {k!r}")
        obj[k] = v
    return obj


def parse_strict_json(raw: str | bytes) -> Any:
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_object_pairs,
            parse_constant=_reject_constant,
        )
    except G2AError:
        raise
    except Exception as exc:
        raise G2AError("invalid_request", 400, f"Invalid RFC 8259 JSON: {exc}") from exc


def validate_strict_json_value(val: Any) -> None:
    if val is None or isinstance(val, (bool, str, int)):
        return
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise G2AError("invalid_request", 400, "Non-finite float is forbidden")
        return
    if isinstance(val, dict):
        for k, child in val.items():
            if not isinstance(k, str):
                raise G2AError("invalid_request", 400, "JSON object keys must be strings")
            validate_strict_json_value(child)
        return
    if isinstance(val, (list, tuple)):
        for item in val:
            validate_strict_json_value(item)
        return
    raise G2AError("invalid_request", 400, f"Unsupported JSON value type: {type(val).__name__}")


def _utf16_sort_key(s: str) -> bytes:
    return s.encode("utf-16-be")


def _format_jcs_float(f: float) -> str:
    if math.isnan(f) or math.isinf(f):
        raise G2AError("invalid_request", 400, "Non-finite float is forbidden")
    if f == 0.0:
        return "0"
    abs_f = abs(f)
    if 1e-6 <= abs_f < 1e21:
        if f.is_integer() and abs_f <= 9007199254740991:
            return str(int(f))
        s = repr(f)
        if "e" not in s and "E" not in s:
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s
        s_fixed = f"{f:.16f}".rstrip("0").rstrip(".")
        return s_fixed
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
        return _c_encode_jcs_string(val)
    if isinstance(val, (list, tuple)):
        return "[" + ",".join(_serialize_jcs(item) for item in val) + "]"
    if isinstance(val, dict):
        keys = val.keys()
        for k in keys:
            if not isinstance(k, str):
                raise G2AError("invalid_request", 400, "JSON object keys must be strings")
        sorted_keys = sorted(keys) if all(k.isascii() for k in keys) else sorted(keys, key=_utf16_sort_key)
        return "{" + ",".join(f"{_c_encode_jcs_string(k)}:{_serialize_jcs(val[k])}" for k in sorted_keys) + "}"
    raise G2AError("invalid_request", 400, f"Cannot canonicalize value of type {type(val).__name__}")


def canonicalize_utf8(val: Any) -> bytes:
    try:
        return _serialize_jcs(val).encode("utf-8")
    except UnicodeEncodeError as uerr:
        raise G2AError("invalid_request", 400, f"Lone UTF-16 surrogate forbidden in I-JSON: {uerr}") from uerr


def jcs_sha256(domain_tag: str, val: Any) -> str:
    canon = canonicalize_utf8(val)
    h = hashlib.sha256()
    h.update(domain_tag.encode("utf-8"))
    h.update(b"\x00")
    h.update(canon)
    return "sha256:" + h.hexdigest()
