package g2acore

import (
	"fmt"
	"net/netip"
	"net/url"
	"strings"
)

var (
	prefixZero4     = netip.MustParsePrefix("0.0.0.0/8")
	prefixCGNAT     = netip.MustParsePrefix("100.64.0.0/10")
	prefixBenchmark = netip.MustParsePrefix("198.18.0.0/15")
	prefix6to4      = netip.MustParsePrefix("2002::/16")
	prefixNAT64     = netip.MustParsePrefix("64:ff9b::/96")
	prefixNAT64Loc  = netip.MustParsePrefix("64:ff9b:1::/48")
	prefixTeredo    = netip.MustParsePrefix("2001::/32")
	prefixDoc6      = netip.MustParsePrefix("2001:db8::/32")
)

// ValidateOutboundURLAgainstSSRF enforces G2A Section 7.1 outbound SSRF & TLS transport policy.
func ValidateOutboundURLAgainstSSRF(rawURL string, allowLoopback bool) error {
	u, err := url.Parse(rawURL)
	if err != nil || u.Host == "" {
		return &G2AError{Code: "invalid_arguments", HTTPStatus: 400, Message: "Malformed Hub endpoint URL"}
	}
	if u.User != nil {
		return &G2AError{Code: "invalid_arguments", HTTPStatus: 400, Message: "Credentials MUST NOT be embedded in endpoint URLs"}
	}
	scheme := strings.ToLower(u.Scheme)
	if scheme != "http" && scheme != "https" {
		return &G2AError{Code: "invalid_arguments", HTTPStatus: 400, Message: "Unsupported URL scheme"}
	}

	host := strings.ToLower(u.Hostname())
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		if !allowLoopback {
			return &G2AError{Code: "permission_denied", HTTPStatus: 403, Message: "Loopback host blocked by SSRF policy"}
		}
		return nil
	}

	if addr, parseErr := netip.ParseAddr(host); parseErr == nil {
		if err := checkIPAddrSSRF(addr, allowLoopback); err != nil {
			return err
		}
		if addr.Unmap().IsLoopback() {
			return nil
		}
	}

	if scheme != "https" {
		return &G2AError{Code: "invalid_arguments", HTTPStatus: 400, Message: "Remote Hub endpoints MUST use https:// (TLS 1.3)"}
	}
	return nil
}

func checkIPAddrSSRF(addr netip.Addr, allowLoopback bool) error {
	if addr.Is4In6() {
		addr = addr.Unmap()
	}
	if addr.Is6() {
		b := addr.As16()
		if prefix6to4.Contains(addr) {
			emb := netip.AddrFrom4([4]byte{b[2], b[3], b[4], b[5]})
			return &G2AError{
				Code:       "permission_denied",
				HTTPStatus: 403,
				Message:    fmt.Sprintf("6to4 transition address %s (embedded %s) blocked by SSRF guard", addr, emb),
			}
		}
		if prefixNAT64.Contains(addr) || prefixNAT64Loc.Contains(addr) {
			emb := netip.AddrFrom4([4]byte{b[12], b[13], b[14], b[15]})
			return &G2AError{
				Code:       "permission_denied",
				HTTPStatus: 403,
				Message:    fmt.Sprintf("NAT64 address %s (embedded %s) blocked by SSRF guard", addr, emb),
			}
		}
		if prefixTeredo.Contains(addr) || prefixDoc6.Contains(addr) {
			return &G2AError{Code: "permission_denied", HTTPStatus: 403, Message: "IPv6 transition/documentation range blocked by SSRF guard"}
		}
	}

	if addr.IsLoopback() {
		if !allowLoopback {
			return &G2AError{Code: "permission_denied", HTTPStatus: 403, Message: "Loopback IP blocked by SSRF guard"}
		}
		return nil
	}
	if addr.IsUnspecified() ||
		addr.IsPrivate() ||
		addr.IsLinkLocalUnicast() ||
		addr.IsLinkLocalMulticast() ||
		addr.IsMulticast() ||
		(addr.Is4() && (prefixZero4.Contains(addr) || prefixCGNAT.Contains(addr) || prefixBenchmark.Contains(addr))) {
		return &G2AError{
			Code:       "permission_denied",
			HTTPStatus: 403,
			Message:    fmt.Sprintf("Non-global address %s blocked by SSRF guard", addr),
		}
	}
	return nil
}
