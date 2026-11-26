//! Strict RFC 8259 JSON parser and RFC 8785 (JCS) canonical serializer with UTF-16 key ordering.

use std::collections::BTreeMap;
use std::fmt::Write as _;

use crate::sha256::domain_sha256_hex;

pub type JsonObject = BTreeMap<String, JsonValue>;
pub type JsonArray = Vec<JsonValue>;

#[derive(Clone, Debug, PartialEq)]
pub enum JsonValue {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Array(JsonArray),
    Object(JsonObject),
}

impl JsonValue {
    pub fn as_object(&self) -> Option<&JsonObject> {
        match self {
            JsonValue::Object(m) => Some(m),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&JsonArray> {
        match self {
            JsonValue::Array(a) => Some(a),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            JsonValue::Str(s) => Some(s.as_str()),
            _ => None,
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            JsonValue::Int(i) => Some(*i),
            JsonValue::Float(f) if f.is_finite() && f.fract() == 0.0 => Some(*f as i64),
            _ => None,
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            JsonValue::Int(i) => Some(*i as f64),
            JsonValue::Float(f) => Some(*f),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            JsonValue::Bool(b) => Some(*b),
            _ => None,
        }
    }
}

impl From<&str> for JsonValue {
    fn from(s: &str) -> Self {
        JsonValue::Str(s.to_owned())
    }
}

impl From<String> for JsonValue {
    fn from(s: String) -> Self {
        JsonValue::Str(s)
    }
}

impl From<bool> for JsonValue {
    fn from(b: bool) -> Self {
        JsonValue::Bool(b)
    }
}

impl From<i64> for JsonValue {
    fn from(i: i64) -> Self {
        JsonValue::Int(i)
    }
}

impl From<f64> for JsonValue {
    fn from(f: f64) -> Self {
        JsonValue::Float(f)
    }
}

impl From<JsonObject> for JsonValue {
    fn from(o: JsonObject) -> Self {
        JsonValue::Object(o)
    }
}

impl From<JsonArray> for JsonValue {
    fn from(a: JsonArray) -> Self {
        JsonValue::Array(a)
    }
}

const MAX_JSON_DEPTH: usize = 64;

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn new(src: &'a str) -> Self {
        Self {
            bytes: src.as_bytes(),
            pos: 0,
        }
    }

    fn skip_ws(&mut self) {
        while self.pos < self.bytes.len() {
            match self.bytes[self.pos] {
                b' ' | b'\t' | b'\r' | b'\n' => self.pos += 1,
                _ => break,
            }
        }
    }

    fn parse_value(&mut self, depth: usize) -> Result<JsonValue, String> {
        if depth > MAX_JSON_DEPTH {
            return Err("JSON nesting depth exceeds 64".to_owned());
        }
        self.skip_ws();
        if self.pos >= self.bytes.len() {
            return Err("Unexpected EOF in JSON".to_owned());
        }
        match self.bytes[self.pos] {
            b'"' => self.parse_string().map(JsonValue::Str),
            b'{' => self.parse_object(depth + 1).map(JsonValue::Object),
            b'[' => self.parse_array(depth + 1).map(JsonValue::Array),
            b't' => {
                if self.bytes[self.pos..].starts_with(b"true") {
                    self.pos += 4;
                    Ok(JsonValue::Bool(true))
                } else {
                    Err("Invalid literal expected true".to_owned())
                }
            }
            b'f' => {
                if self.bytes[self.pos..].starts_with(b"false") {
                    self.pos += 5;
                    Ok(JsonValue::Bool(false))
                } else {
                    Err("Invalid literal expected false".to_owned())
                }
            }
            b'n' => {
                if self.bytes[self.pos..].starts_with(b"null") {
                    self.pos += 4;
                    Ok(JsonValue::Null)
                } else {
                    Err("Invalid literal expected null".to_owned())
                }
            }
            b'-' | b'0'..=b'9' => self.parse_number(),
            other => Err(format!("Unexpected token byte 0x{:02x}", other)),
        }
    }

    fn parse_hex4(&mut self) -> Result<u16, String> {
        if self.pos + 4 > self.bytes.len() {
            return Err("Truncated \\uXXXX escape".to_owned());
        }
        let mut val = 0u16;
        for _ in 0..4 {
            let b = self.bytes[self.pos];
            self.pos += 1;
            let nib = match b {
                b'0'..=b'9' => (b - b'0') as u16,
                b'a'..=b'f' => (b - b'a' + 10) as u16,
                b'A'..=b'F' => (b - b'A' + 10) as u16,
                _ => return Err("Invalid hex digit in \\uXXXX escape".to_owned()),
            };
            val = (val << 4) | nib;
        }
        Ok(val)
    }

    fn parse_string(&mut self) -> Result<String, String> {
        if self.bytes.get(self.pos) != Some(&b'"') {
            return Err("Expected '\"'".to_owned());
        }
        self.pos += 1;
        let mut out = String::new();
        while self.pos < self.bytes.len() {
            let b = self.bytes[self.pos];
            if b == b'"' {
                self.pos += 1;
                return Ok(out);
            }
            if b < 0x20 {
                return Err("Unescaped control character in JSON string".to_owned());
            }
            if b == b'\\' {
                self.pos += 1;
                if self.pos >= self.bytes.len() {
                    return Err("Unterminated escape in JSON string".to_owned());
                }
                let esc = self.bytes[self.pos];
                self.pos += 1;
                match esc {
                    b'"' => out.push('"'),
                    b'\\' => out.push('\\'),
                    b'/' => out.push('/'),
                    b'b' => out.push('\x08'),
                    b'f' => out.push('\x0c'),
                    b'n' => out.push('\n'),
                    b'r' => out.push('\r'),
                    b't' => out.push('\t'),
                    b'u' => {
                        let u1 = self.parse_hex4()?;
                        if (0xD800..=0xDBFF).contains(&u1) {
                            if self.pos + 2 > self.bytes.len()
                                || self.bytes[self.pos] != b'\\'
                                || self.bytes[self.pos + 1] != b'u'
                            {
                                return Err("Lone high UTF-16 surrogate in JSON string".to_owned());
                            }
                            self.pos += 2;
                            let u2 = self.parse_hex4()?;
                            if !(0xDC00..=0xDFFF).contains(&u2) {
                                return Err("Invalid low UTF-16 surrogate in pair".to_owned());
                            }
                            let cp = 0x10000u32 + (((u1 - 0xD800) as u32) << 10) + ((u2 - 0xDC00) as u32);
                            let ch = char::from_u32(cp).ok_or_else(|| "Invalid scalar value".to_owned())?;
                            out.push(ch);
                        } else if (0xDC00..=0xDFFF).contains(&u1) {
                            return Err("Lone low UTF-16 surrogate in JSON string".to_owned());
                        } else {
                            let ch = char::from_u32(u1 as u32).ok_or_else(|| "Invalid BMP code point".to_owned())?;
                            out.push(ch);
                        }
                    }
                    _ => return Err(format!("Invalid JSON escape \\{}", esc as char)),
                }
            } else {
                // Valid UTF-8 slice because input was &str
                let rest = std::str::from_utf8(&self.bytes[self.pos..])
                    .map_err(|_| "Invalid UTF-8 in JSON string".to_owned())?;
                let ch = rest.chars().next().ok_or_else(|| "Unexpected end of UTF-8".to_owned())?;
                out.push(ch);
                self.pos += ch.len_utf8();
            }
        }
        Err("Unterminated JSON string".to_owned())
    }

    fn parse_number(&mut self) -> Result<JsonValue, String> {
        let start = self.pos;
        if self.bytes[self.pos] == b'-' {
            self.pos += 1;
            if self.pos >= self.bytes.len() {
                return Err("Incomplete negative number".to_owned());
            }
        }
        if self.bytes[self.pos] == b'0' {
            self.pos += 1;
            if self.pos < self.bytes.len() && self.bytes[self.pos].is_ascii_digit() {
                return Err("Leading zeros forbidden in JSON number".to_owned());
            }
        } else if self.bytes[self.pos].is_ascii_digit() {
            while self.pos < self.bytes.len() && self.bytes[self.pos].is_ascii_digit() {
                self.pos += 1;
            }
        } else {
            return Err("Invalid number start".to_owned());
        }

        let mut is_float = false;
        if self.pos < self.bytes.len() && self.bytes[self.pos] == b'.' {
            is_float = true;
            self.pos += 1;
            if self.pos >= self.bytes.len() || !self.bytes[self.pos].is_ascii_digit() {
                return Err("Expected digit after decimal point".to_owned());
            }
            while self.pos < self.bytes.len() && self.bytes[self.pos].is_ascii_digit() {
                self.pos += 1;
            }
        }
        if self.pos < self.bytes.len() && (self.bytes[self.pos] == b'e' || self.bytes[self.pos] == b'E') {
            is_float = true;
            self.pos += 1;
            if self.pos < self.bytes.len() && (self.bytes[self.pos] == b'+' || self.bytes[self.pos] == b'-') {
                self.pos += 1;
            }
            if self.pos >= self.bytes.len() || !self.bytes[self.pos].is_ascii_digit() {
                return Err("Expected digit in exponent".to_owned());
            }
            while self.pos < self.bytes.len() && self.bytes[self.pos].is_ascii_digit() {
                self.pos += 1;
            }
        }

        let token = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| "Invalid UTF-8 in number".to_owned())?;
        if !is_float {
            if let Ok(i) = token.parse::<i64>() {
                return Ok(JsonValue::Int(i));
            }
        }
        let f = token.parse::<f64>().map_err(|_| "Invalid float literal".to_owned())?;
        if !f.is_finite() {
            return Err("Non-finite float forbidden in strict JSON".to_owned());
        }
        Ok(JsonValue::Float(f))
    }

    fn parse_array(&mut self, depth: usize) -> Result<JsonArray, String> {
        self.pos += 1; // '['
        self.skip_ws();
        let mut arr = Vec::new();
        if self.pos < self.bytes.len() && self.bytes[self.pos] == b']' {
            self.pos += 1;
            return Ok(arr);
        }
        loop {
            arr.push(self.parse_value(depth)?);
            self.skip_ws();
            if self.pos >= self.bytes.len() {
                return Err("Unterminated JSON array".to_owned());
            }
            match self.bytes[self.pos] {
                b',' => self.pos += 1,
                b']' => {
                    self.pos += 1;
                    return Ok(arr);
                }
                _ => return Err("Expected ',' or ']' in JSON array".to_owned()),
            }
        }
    }

    fn parse_object(&mut self, depth: usize) -> Result<JsonObject, String> {
        self.pos += 1; // '{'
        self.skip_ws();
        let mut map = BTreeMap::new();
        if self.pos < self.bytes.len() && self.bytes[self.pos] == b'}' {
            self.pos += 1;
            return Ok(map);
        }
        loop {
            self.skip_ws();
            let key = self.parse_string()?;
            if map.contains_key(&key) {
                return Err(format!("Duplicate JSON object key: {key}"));
            }
            self.skip_ws();
            if self.bytes.get(self.pos) != Some(&b':') {
                return Err("Expected ':' after object key".to_owned());
            }
            self.pos += 1;
            let val = self.parse_value(depth)?;
            map.insert(key, val);
            self.skip_ws();
            if self.pos >= self.bytes.len() {
                return Err("Unterminated JSON object".to_owned());
            }
            match self.bytes[self.pos] {
                b',' => self.pos += 1,
                b'}' => {
                    self.pos += 1;
                    return Ok(map);
                }
                _ => return Err("Expected ',' or '}' in JSON object".to_owned()),
            }
        }
    }
}

pub fn parse_strict_json(src: &str) -> Result<JsonValue, String> {
    let mut p = Parser::new(src);
    let val = p.parse_value(0)?;
    p.skip_ws();
    if p.pos != p.bytes.len() {
        return Err("Trailing characters after top-level JSON value".to_owned());
    }
    Ok(val)
}

fn write_jcs_string(out: &mut String, s: &str) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\x08' => out.push_str("\\b"),
            '\x09' => out.push_str("\\t"),
            '\x0a' => out.push_str("\\n"),
            '\x0c' => out.push_str("\\f"),
            '\x0d' => out.push_str("\\r"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_jcs_float(out: &mut String, f: f64) -> Result<(), String> {
    if !f.is_finite() {
        return Err("Non-finite float forbidden in RFC 8785 JCS".to_owned());
    }
    if f == 0.0 {
        out.push('0');
        return Ok(());
    }
    let abs_f = f.abs();
    if (1e-6..1e21).contains(&abs_f) {
        if f.fract() == 0.0 && abs_f <= 9007199254740992.0 {
            let _ = write!(out, "{}", f as i64);
        } else {
            let s = f.to_string();
            out.push_str(&s);
        }
    } else {
        // ECMA-262 scientific notation with explicit '+' for positive exponents
        let sci = format!("{:e}", f);
        if let Some((mant, exp_str)) = sci.split_once('e') {
            let exp: i32 = exp_str.parse().unwrap_or(0);
            if exp >= 0 {
                let _ = write!(out, "{mant}e+{exp}");
            } else {
                let _ = write!(out, "{mant}e{exp}");
            }
        } else {
            out.push_str(&sci);
        }
    }
    Ok(())
}

fn write_jcs_value(out: &mut String, val: &JsonValue) -> Result<(), String> {
    match val {
        JsonValue::Null => out.push_str("null"),
        JsonValue::Bool(true) => out.push_str("true"),
        JsonValue::Bool(false) => out.push_str("false"),
        JsonValue::Int(i) => {
            let _ = write!(out, "{i}");
        }
        JsonValue::Float(f) => write_jcs_float(out, *f)?,
        JsonValue::Str(s) => write_jcs_string(out, s),
        JsonValue::Array(items) => {
            out.push('[');
            for (idx, item) in items.iter().enumerate() {
                if idx > 0 {
                    out.push(',');
                }
                write_jcs_value(out, item)?;
            }
            out.push(']');
        }
        JsonValue::Object(map) => {
            out.push('{');
            let mut entries: Vec<(&String, &JsonValue)> = map.iter().collect();
            // RFC 8785 §3.2.3 lexicographic sort by UTF-16 code units
            entries.sort_by(|(a, _), (b, _)| a.encode_utf16().cmp(b.encode_utf16()));
            for (idx, (k, v)) in entries.iter().enumerate() {
                if idx > 0 {
                    out.push(',');
                }
                write_jcs_string(out, k);
                out.push(':');
                write_jcs_value(out, v)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

pub fn canonicalize_utf8(val: &JsonValue) -> Result<String, String> {
    let mut out = String::new();
    write_jcs_value(&mut out, val)?;
    Ok(out)
}

pub fn digest_sha256(domain_tag: &str, val: &JsonValue) -> Result<String, String> {
    let canon = canonicalize_utf8(val)?;
    Ok(domain_sha256_hex(domain_tag, canon.as_bytes()))
}
