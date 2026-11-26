#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <map>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

#include "origin_sdk.hpp"

namespace g2a::http {

inline constexpr const char* kPullExecuteSentinel = "pull://origin.execute";

namespace detail {

#ifdef _WIN32
using SocketHandle = SOCKET;
inline constexpr SocketHandle kInvalidSocket = INVALID_SOCKET;
inline void close_socket(SocketHandle s) {
    if (s != INVALID_SOCKET) ::closesocket(s);
}
struct WinsockInitializer {
    WinsockInitializer() {
        WSADATA wsa{};
        (void)::WSAStartup(MAKEWORD(2, 2), &wsa);
    }
};
inline void ensure_socket_subsystem() {
    static WinsockInitializer init;
    (void)init;
}
#else
using SocketHandle = int;
inline constexpr SocketHandle kInvalidSocket = -1;
inline void close_socket(SocketHandle s) {
    if (s >= 0) ::close(s);
}
inline void ensure_socket_subsystem() {}
#endif

inline int64_t unix_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

inline bool constant_time_equals(const std::string& a, const std::string& b) {
    if (a.size() != b.size()) return false;
    unsigned char diff = 0;
    for (size_t i = 0; i < a.size(); ++i) {
        diff |= static_cast<unsigned char>(a[i]) ^ static_cast<unsigned char>(b[i]);
    }
    return diff == 0;
}

struct ParsedHttpUrl {
    std::string host;
    uint16_t port = 80;
    std::string base_path;
};

inline ParsedHttpUrl parse_http_url(const std::string& raw_url) {
    std::string s = raw_url;
    while (!s.empty() && s.back() == '/') s.pop_back();
    const std::string prefix = "http://";
    if (s.rfind(prefix, 0) != 0) {
        throw std::invalid_argument("Unsupported non-http URL: " + raw_url);
    }
    std::string rest = s.substr(prefix.size());
    std::string host_port = rest;
    std::string base_path;
    size_t slash = rest.find('/');
    if (slash != std::string::npos) {
        host_port = rest.substr(0, slash);
        base_path = rest.substr(slash);
    }
    ParsedHttpUrl out;
    size_t colon = host_port.rfind(':');
    if (colon != std::string::npos) {
        out.host = host_port.substr(0, colon);
        out.port = static_cast<uint16_t>(std::stoi(host_port.substr(colon + 1)));
    } else {
        out.host = host_port;
        out.port = 80;
    }
    if (out.host == "localhost") out.host = "127.0.0.1";
    out.base_path = std::move(base_path);
    return out;
}

inline SocketHandle connect_ipv4(const std::string& ip, uint16_t port) {
    ensure_socket_subsystem();
    SocketHandle sock = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock == kInvalidSocket) return kInvalidSocket;

    int one = 1;
    (void)::setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&one), sizeof(one));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    if (::inet_pton(AF_INET, ip.c_str(), &addr.sin_addr) != 1) {
        close_socket(sock);
        return kInvalidSocket;
    }
    if (::connect(sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        close_socket(sock);
        return kInvalidSocket;
    }
    return sock;
}

inline bool send_all(SocketHandle sock, const char* data, size_t len) {
    size_t sent = 0;
    while (sent < len) {
#ifdef _WIN32
        int n = ::send(sock, data + sent, static_cast<int>(len - sent), 0);
#else
        ssize_t n = ::send(sock, data + sent, len - sent, 0);
#endif
        if (n <= 0) return false;
        sent += static_cast<size_t>(n);
    }
    return true;
}

inline std::string to_lower_ascii(std::string s) {
    for (char& c : s) {
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    }
    return s;
}

inline std::string trim_ascii(const std::string& s) {
    size_t a = 0;
    while (a < s.size() && (s[a] == ' ' || s[a] == '\t' || s[a] == '\r' || s[a] == '\n')) ++a;
    size_t b = s.size();
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t' || s[b - 1] == '\r' || s[b - 1] == '\n')) --b;
    return s.substr(a, b - a);
}

struct HttpRawMessage {
    std::string first_line;
    std::map<std::string, std::string> headers;
    std::string body;
};

inline bool read_http_message(SocketHandle sock, std::string& leftover, HttpRawMessage& out) {
    char buf[4096];
    size_t hdr_end = std::string::npos;
    while ((hdr_end = leftover.find("\r\n\r\n")) == std::string::npos) {
        if (leftover.size() > 65536) return false;
#ifdef _WIN32
        int n = ::recv(sock, buf, sizeof(buf), 0);
#else
        ssize_t n = ::recv(sock, buf, sizeof(buf), 0);
#endif
        if (n <= 0) return false;
        leftover.append(buf, static_cast<size_t>(n));
    }

    std::string hdr_block = leftover.substr(0, hdr_end);
    leftover.erase(0, hdr_end + 4);

    out.headers.clear();
    size_t line_start = 0;
    bool first = true;
    while (line_start < hdr_block.size()) {
        size_t nl = hdr_block.find("\r\n", line_start);
        std::string ln = (nl == std::string::npos)
                             ? hdr_block.substr(line_start)
                             : hdr_block.substr(line_start, nl - line_start);
        if (first) {
            out.first_line = ln;
            first = false;
        } else {
            size_t colon = ln.find(':');
            if (colon != std::string::npos) {
                out.headers[to_lower_ascii(trim_ascii(ln.substr(0, colon)))] = trim_ascii(ln.substr(colon + 1));
            }
        }
        if (nl == std::string::npos) break;
        line_start = nl + 2;
    }

    size_t content_len = 0;
    auto cl_it = out.headers.find("content-length");
    if (cl_it != out.headers.end()) {
        content_len = static_cast<size_t>(std::stoull(cl_it->second));
        if (content_len > (4u << 20)) return false;
    }
    while (leftover.size() < content_len) {
#ifdef _WIN32
        int n = ::recv(sock, buf, sizeof(buf), 0);
#else
        ssize_t n = ::recv(sock, buf, sizeof(buf), 0);
#endif
        if (n <= 0) return false;
        leftover.append(buf, static_cast<size_t>(n));
    }
    out.body = leftover.substr(0, content_len);
    leftover.erase(0, content_len);
    return true;
}

inline OriginExecuteRequest parse_execute_payload(const JsonObject& p) {
    OriginExecuteRequest req;
    req.slot_id = std::get<std::string>(p.at("slot_id").data);
    if (auto it = p.find("epoch"); it != p.end()) req.epoch = std::get<int64_t>(it->second.data);
    if (auto it = p.find("kind"); it != p.end()) req.kind = std::get<std::string>(it->second.data);
    req.execution_id = std::get<std::string>(p.at("execution_id").data);
    if (auto it = p.find("method_id"); it != p.end()) {
        if (const auto* s = std::get_if<std::string>(&it->second.data)) req.method_id = *s;
    }
    if (auto it = p.find("actor"); it != p.end()) {
        if (const auto* s = std::get_if<std::string>(&it->second.data)) req.actor = *s;
    }
    if (auto it = p.find("args"); it != p.end()) {
        if (const auto* a = std::get_if<JsonObject>(&it->second.data)) req.args = *a;
    }
    return req;
}

} // namespace detail

// Persistent HTTP/1.1 Keep-Alive client posting Origin Interface frames to Intelligent Hub.
class HttpHubTransportClient {
public:
    explicit HttpHubTransportClient(const std::string& hub_origin_url, std::string shared_token = "")
        : parsed_(detail::parse_http_url(hub_origin_url)),
          shared_token_(std::move(shared_token)) {}

    ~HttpHubTransportClient() {
        std::lock_guard<std::mutex> lk(mu_);
        detail::close_socket(sock_);
        sock_ = detail::kInvalidSocket;
    }

    HttpHubTransportClient(const HttpHubTransportClient&) = delete;
    HttpHubTransportClient& operator=(const HttpHubTransportClient&) = delete;

    void post_to_hub(const std::string& operation, const JsonValue& payload) {
        std::lock_guard<std::mutex> lk(mu_);
        ++seq_;
        JsonObject env;
        env.emplace("g2a_version", std::string(OriginSdk::kWireVersion));
        env.emplace("request_id", "cpp-orig-" + std::to_string(seq_));
        env.emplace("timestamp_ms", detail::unix_ms());
        env.emplace("operation", operation);
        env.emplace("payload", payload);
        env.emplace("extensions", JsonObject{});

        std::string body = jcs::canonicalize_utf8(env);
        std::string path = parsed_.base_path + "/g2a/v1/origin";
        std::string req = "POST " + path + " HTTP/1.1\r\n"
                        + "Host: " + parsed_.host + ":" + std::to_string(parsed_.port) + "\r\n"
                        + "Content-Type: application/json\r\n"
                        + "Connection: keep-alive\r\n";
        if (!shared_token_.empty()) {
            req += "Authorization: Bearer " + shared_token_ + "\r\n";
        }
        req += "Content-Length: " + std::to_string(body.size()) + "\r\n\r\n";

        for (int attempt = 0; attempt < 2; ++attempt) {
            if (sock_ == detail::kInvalidSocket) {
                sock_ = detail::connect_ipv4(parsed_.host, parsed_.port);
                leftover_.clear();
                if (sock_ == detail::kInvalidSocket) {
                    if (attempt == 1) throw std::runtime_error("Failed to connect to Intelligent Hub");
                    continue;
                }
            }
            if (!detail::send_all(sock_, req.data(), req.size()) ||
                !detail::send_all(sock_, body.data(), body.size())) {
                detail::close_socket(sock_);
                sock_ = detail::kInvalidSocket;
                if (attempt == 1) throw std::runtime_error("Failed writing HTTP request to Hub");
                continue;
            }
            detail::HttpRawMessage resp;
            if (!detail::read_http_message(sock_, leftover_, resp)) {
                detail::close_socket(sock_);
                sock_ = detail::kInvalidSocket;
                if (attempt == 1) throw std::runtime_error("Failed reading HTTP reply from Hub");
                continue;
            }
            auto conn_it = resp.headers.find("connection");
            if (conn_it != resp.headers.end() && detail::to_lower_ascii(conn_it->second) == "close") {
                detail::close_socket(sock_);
                sock_ = detail::kInvalidSocket;
            }
            if (resp.first_line.find(" 200 ") == std::string::npos) {
                throw std::runtime_error("Hub rejected Origin frame: " + resp.first_line + " " + resp.body);
            }
            return;
        }
    }

private:
    detail::ParsedHttpUrl parsed_;
    std::string shared_token_;
    std::mutex mu_;
    detail::SocketHandle sock_ = detail::kInvalidSocket;
    std::string leftover_;
    uint64_t seq_ = 0;
};

// Persistent HTTP/1.1 Keep-Alive loopback execute listener for C++17 / Unreal Game Hosts.
class LoopbackExecuteListener {
public:
    explicit LoopbackExecuteListener(OriginSdk& sdk, uint16_t port = 0, std::string shared_token = "")
        : sdk_(sdk), shared_token_(std::move(shared_token)) {
        detail::ensure_socket_subsystem();
        listen_sock_ = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (listen_sock_ == detail::kInvalidSocket) {
            throw std::runtime_error("Failed to create loopback TCP socket");
        }
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(port);
        (void)::inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
        if (::bind(listen_sock_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 ||
            ::listen(listen_sock_, 16) != 0) {
            detail::close_socket(listen_sock_);
            throw std::runtime_error("Failed to bind/listen on 127.0.0.1");
        }
        sockaddr_in bound{};
#ifdef _WIN32
        int len = sizeof(bound);
#else
        socklen_t len = sizeof(bound);
#endif
        (void)::getsockname(listen_sock_, reinterpret_cast<sockaddr*>(&bound), &len);
        bound_port_ = ntohs(bound.sin_port);
        execute_url_ = "http://127.0.0.1:" + std::to_string(bound_port_) + "/g2a/v1/origin/execute";
        accept_thread_ = std::thread([this]() { accept_loop(); });
    }

    ~LoopbackExecuteListener() {
        stop_.store(true);
        detail::close_socket(listen_sock_);
        listen_sock_ = detail::kInvalidSocket;
        detail::SocketHandle wake = detail::connect_ipv4("127.0.0.1", bound_port_);
        detail::close_socket(wake);
        if (accept_thread_.joinable()) accept_thread_.join();
    }

    LoopbackExecuteListener(const LoopbackExecuteListener&) = delete;
    LoopbackExecuteListener& operator=(const LoopbackExecuteListener&) = delete;

    const std::string& execute_url() const { return execute_url_; }
    uint16_t bound_port() const { return bound_port_; }

private:
    void accept_loop() {
        while (!stop_.load()) {
            detail::SocketHandle cli = ::accept(listen_sock_, nullptr, nullptr);
            if (cli == detail::kInvalidSocket) break;
            if (stop_.load()) {
                detail::close_socket(cli);
                break;
            }
            std::thread(&LoopbackExecuteListener::handle_client, this, cli).detach();
        }
    }

    void handle_client(detail::SocketHandle cli) {
        int one = 1;
        (void)::setsockopt(cli, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&one), sizeof(one));
        std::string leftover;
        while (!stop_.load()) {
            detail::HttpRawMessage msg;
            if (!detail::read_http_message(cli, leftover, msg)) break;
            bool want_close = false;
            if (auto it = msg.headers.find("connection"); it != msg.headers.end()) {
                want_close = (detail::to_lower_ascii(it->second) == "close");
            }

            if (!shared_token_.empty()) {
                std::string expected = "Bearer " + shared_token_;
                auto auth_it = msg.headers.find("authorization");
                if (auth_it == msg.headers.end() || !detail::constant_time_equals(auth_it->second, expected)) {
                    write_reply(cli, 401, "", "not_accepted", JsonNull{}, "unauthenticated", "Invalid Origin token", want_close);
                    if (want_close) break;
                    continue;
                }
            }

            std::string req_id;
            try {
                JsonValue root = jcs::parse_strict_json(msg.body);
                const auto& obj = std::get<JsonObject>(root.data);
                if (auto r_it = obj.find("request_id"); r_it != obj.end()) {
                    req_id = std::get<std::string>(r_it->second.data);
                }
                std::string op = std::get<std::string>(obj.at("operation").data);
                if (op != "origin.execute") {
                    write_reply(cli, 400, req_id, "not_accepted", JsonNull{}, "invalid_request", "Expected origin.execute", want_close);
                } else {
                    const auto& p_obj = std::get<JsonObject>(obj.at("payload").data);
                    OriginExecuteReply rep = sdk_.handle_origin_execute(detail::parse_execute_payload(p_obj));
                    write_reply(cli, 200, req_id, "accepted", rep.to_json(), "", "", want_close);
                }
            } catch (const std::exception& ex) {
                write_reply(cli, 400, req_id, "not_accepted", JsonNull{}, "invalid_request", ex.what(), want_close);
            }
            if (want_close) break;
        }
        detail::close_socket(cli);
    }

    static void write_reply(
        detail::SocketHandle cli,
        int status,
        const std::string& req_id,
        const std::string& acceptance,
        const JsonValue& result,
        const std::string& err_code,
        const std::string& err_msg,
        bool close_conn) {
        JsonObject env;
        env.emplace("g2a_version", std::string(OriginSdk::kWireVersion));
        env.emplace("request_id", req_id);
        env.emplace("timestamp_ms", detail::unix_ms());
        env.emplace("acceptance", acceptance);
        env.emplace("result", result);
        if (err_code.empty()) {
            env.emplace("error", JsonNull{});
        } else {
            JsonObject e;
            e.emplace("code", err_code);
            e.emplace("http_status", static_cast<int64_t>(status));
            e.emplace("message", err_msg);
            env.emplace("error", std::move(e));
        }
        std::string payload = jcs::canonicalize_utf8(env);
        std::string head = "HTTP/1.1 " + std::to_string(status) + (status == 200 ? " OK\r\n" : " Error\r\n")
                         + "Content-Type: application/json\r\n"
                         + "Content-Length: " + std::to_string(payload.size()) + "\r\n"
                         + "Connection: " + std::string(close_conn ? "close" : "keep-alive") + "\r\n\r\n";
        (void)detail::send_all(cli, head.data(), head.size());
        (void)detail::send_all(cli, payload.data(), payload.size());
    }

    OriginSdk& sdk_;
    std::string shared_token_;
    detail::SocketHandle listen_sock_ = detail::kInvalidSocket;
    uint16_t bound_port_ = 0;
    std::string execute_url_;
    std::atomic<bool> stop_{false};
    std::thread accept_thread_;
};

// One-step RAII C++17 Origin Host managing Hub discovery, Keep-Alive transport, loopback listener, & offline teardown.
class ScopedOriginHost {
public:
    ScopedOriginHost(
        std::string slot_id,
        const std::function<void(OriginSdk&)>& setup_fn,
        const std::string& hub_origin_url = "auto",
        const std::string& fallback_url = "http://127.0.0.1:19210",
        const std::string& shared_token = "",
        std::string role_kind = "companion",
        std::string billing_mode = "player_metered") {
        std::string resolved = hub_origin_url;
        if (hub_origin_url == "auto") {
            try {
                resolved = OriginSdk::discover_local_hub_origin_url();
            } catch (...) {
                resolved = fallback_url;
            }
        }
        transport_ = std::make_unique<HttpHubTransportClient>(resolved, shared_token);
        sdk_ = std::make_unique<OriginSdk>(
            std::move(slot_id),
            [this](const std::string& op, const JsonValue& p) { transport_->post_to_hub(op, p); },
            std::move(role_kind),
            std::move(billing_mode));
        sdk_->bind_actor_subject("companion");
        if (sdk_ && role_kind != "independent_npc") sdk_->bind_actor_subject("player");
        listener_ = std::make_unique<LoopbackExecuteListener>(*sdk_, 0, shared_token);
        if (setup_fn) setup_fn(*sdk_);
        sdk_->publish_capabilities_and_baseline(listener_->execute_url());
    }

    ~ScopedOriginHost() {
        if (sdk_) {
            try { sdk_->report_player_presence(false, 0); } catch (...) {}
        }
        listener_.reset();
        sdk_.reset();
        transport_.reset();
    }

    ScopedOriginHost(const ScopedOriginHost&) = delete;
    ScopedOriginHost& operator=(const ScopedOriginHost&) = delete;

    OriginSdk& sdk() { return *sdk_; }
    const std::string& execute_url() const { return listener_->execute_url(); }

private:
    std::unique_ptr<HttpHubTransportClient> transport_;
    std::unique_ptr<OriginSdk> sdk_;
    std::unique_ptr<LoopbackExecuteListener> listener_;
};

} // namespace g2a::http
