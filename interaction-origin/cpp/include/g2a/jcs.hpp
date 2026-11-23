#pragma once

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

struct JsonValue;
using JsonArray = std::vector<JsonValue>;
using JsonObject = std::map<std::string, JsonValue>;

struct JsonValue {
    using Storage = std::variant<std::nullptr_t, bool, int64_t, double, std::string, JsonArray, JsonObject>;
    Storage data;

    JsonValue() : data(nullptr) {}
    JsonValue(std::nullptr_t) : data(nullptr) {}
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

inline void append_canonical(std::string& out, const JsonValue& v) {
    if (std::holds_alternative<std::nullptr_t>(v.data)) {
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
        // std::map orders keys lexicographically by UTF-8 bytes (identical to UTF-16 code units across BMP/ASCII keys).
        out.push_back('{');
        bool first = true;
        for (const auto& [k, child] : *obj) {
            if (!first) out.push_back(',');
            first = false;
            append_escaped_string(out, k);
            out.push_back(':');
            append_canonical(out, child);
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

} // namespace jcs
} // namespace g2a
