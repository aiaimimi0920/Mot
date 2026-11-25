#pragma once

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <variant>
#include <vector>

#include "sha256.hpp"

namespace g2a {

struct JsonNull {
    uint64_t pad_ = 0;
    constexpr bool operator==(const JsonNull&) const noexcept { return true; }
    constexpr bool operator!=(const JsonNull&) const noexcept { return false; }
};

struct JsonValue;
using JsonArray = std::vector<JsonValue>;
using JsonObject = std::map<std::string, JsonValue>;

struct JsonValue {
    using Storage = std::variant<JsonNull, bool, int64_t, double, std::string, JsonArray, JsonObject>;
    Storage data;

    JsonValue() : data(JsonNull{}) {}
    JsonValue(std::nullptr_t) : data(JsonNull{}) {}
    JsonValue(JsonNull n) : data(n) {}
    JsonValue(bool b) : data(b) {}
    JsonValue(int i) : data(static_cast<int64_t>(i)) {}
    JsonValue(int64_t i) : data(i) {}
    JsonValue(double d) : data(d) {
        if (std::isnan(d) || std::isinf(d)) {
            throw std::invalid_argument("G2A strict JSON forbids NaN or Infinity");
        }
    }
    JsonValue(const char* s) : data(std::string(s)) {}
    JsonValue(std::string s) : data(std::move(s)) {}
    JsonValue(JsonArray a) : data(std::move(a)) {}
    JsonValue(JsonObject o) : data(std::move(o)) {}

    bool is_null() const noexcept { return std::holds_alternative<JsonNull>(data); }
};

namespace jcs {

inline void append_escaped_string(std::string& out, const std::string& s) {
    out.push_back('"');
    for (unsigned char ch : s) {
        switch (ch) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (ch <= 0x1F) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned int>(ch));
                    out += buf;
                } else {
                    out.push_back(static_cast<char>(ch));
                }
        }
    }
    out.push_back('"');
}

inline std::string format_rfc8785_double(double f) {
    if (std::isnan(f) || std::isinf(f)) {
        throw std::invalid_argument("G2A strict JSON forbids NaN or Infinity");
    }
    if (f == 0.0) return "0";
    double abs_f = std::fabs(f);
    if (abs_f >= 1e-6 && abs_f < 1e21) {
        if (std::trunc(f) == f && abs_f <= 9007199254740991.0) {
            return std::to_string(static_cast<int64_t>(f));
        }
        std::ostringstream oss;
        oss << std::setprecision(16) << f;
        std::string s = oss.str();
        if (s.find('e') == std::string::npos && s.find('E') == std::string::npos) {
            if (s.find('.') != std::string::npos) {
                while (!s.empty() && s.back() == '0') s.pop_back();
                if (!s.empty() && s.back() == '.') s.pop_back();
            }
            return s;
        }
    }
    std::ostringstream oss;
    oss << std::scientific << std::setprecision(15) << f;
    std::string s = oss.str();
    auto e_pos = s.find('e');
    if (e_pos == std::string::npos) e_pos = s.find('E');
    std::string mant = s.substr(0, e_pos);
    while (!mant.empty() && mant.back() == '0') mant.pop_back();
    if (!mant.empty() && mant.back() == '.') mant.pop_back();
    int exp_val = std::stoi(s.substr(e_pos + 1));
    return mant + "e" + (exp_val >= 0 ? "+" : "") + std::to_string(exp_val);
}

inline bool is_ascii_str(const std::string& s) {
    for (unsigned char c : s) {
        if (c >= 0x80) return false;
    }
    return true;
}

inline std::vector<uint16_t> utf8_to_utf16_units(const std::string& s) {
    std::vector<uint16_t> out;
    out.reserve(s.size());
    size_t i = 0;
    const size_t n = s.size();
    while (i < n) {
        unsigned char b0 = static_cast<unsigned char>(s[i]);
        uint32_t cp = 0;
        if (b0 < 0x80) {
            cp = b0;
            i += 1;
        } else if ((b0 & 0xE0) == 0xC0 && i + 1 < n) {
            cp = ((b0 & 0x1F) << 6) | (static_cast<unsigned char>(s[i + 1]) & 0x3F);
            i += 2;
        } else if ((b0 & 0xF0) == 0xE0 && i + 2 < n) {
            cp = ((b0 & 0x0F) << 12) |
                 ((static_cast<unsigned char>(s[i + 1]) & 0x3F) << 6) |
                 (static_cast<unsigned char>(s[i + 2]) & 0x3F);
            i += 3;
        } else if ((b0 & 0xF8) == 0xF0 && i + 3 < n) {
            cp = ((b0 & 0x07) << 18) |
                 ((static_cast<unsigned char>(s[i + 1]) & 0x3F) << 12) |
                 ((static_cast<unsigned char>(s[i + 2]) & 0x3F) << 6) |
                 (static_cast<unsigned char>(s[i + 3]) & 0x3F);
            i += 4;
        } else {
            throw std::invalid_argument("Invalid UTF-8 sequence in JCS key");
        }
        if (cp <= 0xFFFF) {
            out.push_back(static_cast<uint16_t>(cp));
        } else {
            uint32_t adj = cp - 0x10000;
            out.push_back(static_cast<uint16_t>(0xD800 + (adj >> 10)));
            out.push_back(static_cast<uint16_t>(0xDC00 + (adj & 0x3FF)));
        }
    }
    return out;
}

inline bool less_utf16_jcs_key(const std::string& a, const std::string& b) {
    if (is_ascii_str(a) && is_ascii_str(b)) {
        return a < b;
    }
    auto ua = utf8_to_utf16_units(a);
    auto ub = utf8_to_utf16_units(b);
    return std::lexicographical_compare(ua.begin(), ua.end(), ub.begin(), ub.end());
}

inline void append_canonical(std::string& out, const JsonValue& v) {
    if (v.is_null()) {
        out += "null";
    } else if (const auto* b = std::get_if<bool>(&v.data)) {
        out += (*b ? "true" : "false");
    } else if (const auto* i = std::get_if<int64_t>(&v.data)) {
        out += std::to_string(*i);
    } else if (const auto* d = std::get_if<double>(&v.data)) {
        out += format_rfc8785_double(*d);
    } else if (const auto* s = std::get_if<std::string>(&v.data)) {
        append_escaped_string(out, *s);
    } else if (const auto* arr = std::get_if<JsonArray>(&v.data)) {
        out.push_back('[');
        for (size_t idx = 0; idx < arr->size(); ++idx) {
            if (idx > 0) out.push_back(',');
            append_canonical(out, (*arr)[idx]);
        }
        out.push_back(']');
    } else if (const auto* obj = std::get_if<JsonObject>(&v.data)) {
        bool all_ascii = true;
        for (const auto& [k, _] : *obj) {
            if (!is_ascii_str(k)) {
                all_ascii = false;
                break;
            }
        }
        out.push_back('{');
        if (all_ascii) {
            bool first = true;
            for (const auto& [k, child] : *obj) {
                if (!first) out.push_back(',');
                first = false;
                append_escaped_string(out, k);
                out.push_back(':');
                append_canonical(out, child);
            }
        } else {
            std::vector<const JsonObject::value_type*> entries;
            entries.reserve(obj->size());
            for (const auto& kv : *obj) entries.push_back(&kv);
            std::sort(entries.begin(), entries.end(), [](const auto* lhs, const auto* rhs) {
                return less_utf16_jcs_key(lhs->first, rhs->first);
            });
            for (size_t idx = 0; idx < entries.size(); ++idx) {
                if (idx > 0) out.push_back(',');
                append_escaped_string(out, entries[idx]->first);
                out.push_back(':');
                append_canonical(out, entries[idx]->second);
            }
        }
        out.push_back('}');
    }
}

inline std::string canonicalize_utf8(const JsonValue& v) {
    std::string out;
    append_canonical(out, v);
    return out;
}

inline std::string digest_sha256(const std::string& domain_tag, const JsonValue& v) {
    std::string canon = canonicalize_utf8(v);
    crypto::Sha256 sha;
    sha.update(domain_tag);
    const uint8_t zero = 0;
    sha.update(&zero, 1);
    sha.update(canon);
    return "sha256:" + sha.finish_hex();
}

namespace detail {

class StrictJsonParser {
public:
    explicit StrictJsonParser(const std::string& s) : s_(s), pos_(0) {}

    JsonValue parse_document() {
        skip_ws();
        JsonValue val = parse_value();
        skip_ws();
        if (pos_ != s_.size()) {
            throw std::invalid_argument("Trailing characters after strict JSON document");
        }
        return val;
    }

private:
    const std::string& s_;
    size_t pos_;

    void skip_ws() {
        while (pos_ < s_.size() && (s_[pos_] == ' ' || s_[pos_] == '\t' || s_[pos_] == '\r' || s_[pos_] == '\n')) {
            ++pos_;
        }
    }

    char peek() const {
        if (pos_ >= s_.size()) throw std::invalid_argument("Unexpected end of JSON input");
        return s_[pos_];
    }

    char get() {
        char c = peek();
        ++pos_;
        return c;
    }

    JsonValue parse_value() {
        skip_ws();
        char c = peek();
        if (c == '"') return JsonValue(parse_string());
        if (c == '{') return parse_object();
        if (c == '[') return parse_array();
        if (c == 't') { expect_literal("true"); return JsonValue(true); }
        if (c == 'f') { expect_literal("false"); return JsonValue(false); }
        if (c == 'n') { expect_literal("null"); return JsonValue(nullptr); }
        if (c == '-' || (c >= '0' && c <= '9')) return parse_number();
        throw std::invalid_argument("Unexpected token in strict JSON");
    }

    void expect_literal(const char* lit) {
        for (const char* p = lit; *p; ++p) {
            if (get() != *p) throw std::invalid_argument("Invalid JSON literal");
        }
    }

    std::string parse_string() {
        if (get() != '"') throw std::invalid_argument("Expected '\"'");
        std::string out;
        while (true) {
            char c = get();
            if (c == '"') return out;
            if (static_cast<unsigned char>(c) < 0x20) {
                throw std::invalid_argument("Unescaped control character in JSON string");
            }
            if (c == '\\') {
                char esc = get();
                switch (esc) {
                    case '"': out.push_back('"'); break;
                    case '\\': out.push_back('\\'); break;
                    case '/': out.push_back('/'); break;
                    case 'b': out.push_back('\b'); break;
                    case 'f': out.push_back('\f'); break;
                    case 'n': out.push_back('\n'); break;
                    case 'r': out.push_back('\r'); break;
                    case 't': out.push_back('\t'); break;
                    case 'u': {
                        uint32_t code = read_hex4();
                        if (code >= 0xDC00 && code <= 0xDFFF) {
                            throw std::invalid_argument("Lone low UTF-16 surrogate forbidden in I-JSON");
                        }
                        if (code >= 0xD800 && code <= 0xDBFF) {
                            if (get() != '\\' || get() != 'u') {
                                throw std::invalid_argument("Lone high UTF-16 surrogate forbidden in I-JSON");
                            }
                            uint32_t low = read_hex4();
                            if (low < 0xDC00 || low > 0xDFFF) {
                                throw std::invalid_argument("Invalid low UTF-16 surrogate in pair");
                            }
                            code = 0x10000u + (((code - 0xD800u) << 10) | (low - 0xDC00u));
                        }
                        append_utf8_codepoint(out, code);
                        break;
                    }
                    default:
                        throw std::invalid_argument("Invalid escape sequence in JSON string");
                }
            } else {
                out.push_back(c);
            }
        }
    }

    uint32_t read_hex4() {
        uint32_t code = 0;
        for (int i = 0; i < 4; ++i) {
            char h = get();
            code <<= 4;
            if (h >= '0' && h <= '9') code |= static_cast<uint32_t>(h - '0');
            else if (h >= 'a' && h <= 'f') code |= static_cast<uint32_t>(h - 'a' + 10);
            else if (h >= 'A' && h <= 'F') code |= static_cast<uint32_t>(h - 'A' + 10);
            else throw std::invalid_argument("Invalid \\uXXXX hex escape");
        }
        return code;
    }

    static void append_utf8_codepoint(std::string& out, uint32_t code) {
        if (code <= 0x7F) {
            out.push_back(static_cast<char>(code));
        } else if (code <= 0x7FF) {
            out.push_back(static_cast<char>(0xC0 | (code >> 6)));
            out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
        } else if (code <= 0xFFFF) {
            out.push_back(static_cast<char>(0xE0 | (code >> 12)));
            out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
        } else {
            out.push_back(static_cast<char>(0xF0 | (code >> 18)));
            out.push_back(static_cast<char>(0x80 | ((code >> 12) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
        }
    }

    JsonValue parse_number() {
        size_t start = pos_;
        if (s_[pos_] == '-') ++pos_;
        if (pos_ >= s_.size() || !std::isdigit(static_cast<unsigned char>(s_[pos_]))) {
            throw std::invalid_argument("Invalid JSON number");
        }
        if (s_[pos_] == '0') {
            ++pos_;
            if (pos_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[pos_]))) {
                throw std::invalid_argument("Leading zero forbidden in strict JSON number");
            }
        } else {
            while (pos_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[pos_]))) ++pos_;
        }
        bool is_float = false;
        if (pos_ < s_.size() && s_[pos_] == '.') {
            is_float = true;
            ++pos_;
            if (pos_ >= s_.size() || !std::isdigit(static_cast<unsigned char>(s_[pos_]))) {
                throw std::invalid_argument("Invalid fraction in JSON number");
            }
            while (pos_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[pos_]))) ++pos_;
        }
        if (pos_ < s_.size() && (s_[pos_] == 'e' || s_[pos_] == 'E')) {
            is_float = true;
            ++pos_;
            if (pos_ < s_.size() && (s_[pos_] == '+' || s_[pos_] == '-')) ++pos_;
            if (pos_ >= s_.size() || !std::isdigit(static_cast<unsigned char>(s_[pos_]))) {
                throw std::invalid_argument("Invalid exponent in JSON number");
            }
            while (pos_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[pos_]))) ++pos_;
        }
        std::string token = s_.substr(start, pos_ - start);
        if (!is_float) {
            return JsonValue(static_cast<int64_t>(std::stoll(token)));
        }
        return JsonValue(std::stod(token));
    }

    JsonValue parse_array() {
        get(); // '['
        JsonArray arr;
        skip_ws();
        if (peek() == ']') {
            get();
            return JsonValue(std::move(arr));
        }
        while (true) {
            arr.push_back(parse_value());
            skip_ws();
            char sep = get();
            if (sep == ']') break;
            if (sep != ',') throw std::invalid_argument("Expected ',' or ']' in JSON array");
        }
        return JsonValue(std::move(arr));
    }

    JsonValue parse_object() {
        get(); // '{'
        JsonObject obj;
        skip_ws();
        if (peek() == '}') {
            get();
            return JsonValue(std::move(obj));
        }
        while (true) {
            skip_ws();
            std::string key = parse_string();
            if (obj.find(key) != obj.end()) {
                throw std::invalid_argument("Duplicate object key forbidden in G2A strict JSON: " + key);
            }
            skip_ws();
            if (get() != ':') throw std::invalid_argument("Expected ':' after JSON key");
            obj.emplace(std::move(key), parse_value());
            skip_ws();
            char sep = get();
            if (sep == '}') break;
            if (sep != ',') throw std::invalid_argument("Expected ',' or '}' in JSON object");
        }
        return JsonValue(std::move(obj));
    }
};

} // namespace detail

inline JsonValue parse_strict_json(const std::string& raw) {
    return detail::StrictJsonParser(raw).parse_document();
}

} // namespace jcs
} // namespace g2a
