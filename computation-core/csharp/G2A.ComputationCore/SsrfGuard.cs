using System.Net;
using System.Net.Sockets;

namespace G2A.ComputationCore;

/// <summary>
/// Enforces G2A Section 7.1 Outbound SSRF & TLS 1.3 transport policy for Computation Core endpoints.
/// </summary>
public static class SsrfGuard
{
    public static void ValidateOutboundUrlAgainstSsrf(string rawUrl, bool allowLoopback = false)
    {
        if (!Uri.TryCreate(rawUrl, UriKind.Absolute, out var uri))
            throw new G2ACoreException("invalid_arguments", 400, $"Invalid Hub endpoint URL: {rawUrl}");
        if (!string.IsNullOrEmpty(uri.UserInfo))
            throw new G2ACoreException("invalid_arguments", 400, "Credentials MUST NOT be embedded in endpoint URLs");

        string scheme = uri.Scheme.ToLowerInvariant();
        if (scheme != "http" && scheme != "https")
            throw new G2ACoreException("invalid_arguments", 400, $"Unsupported URL scheme '{scheme}'");

        string host = uri.DnsSafeHost.ToLowerInvariant();
        if (host == "localhost" || host.EndsWith(".localhost", StringComparison.Ordinal))
        {
            if (!allowLoopback)
                throw new G2ACoreException("permission_denied", 403, "Loopback host blocked by SSRF policy");
            return;
        }

        if (IPAddress.TryParse(host, out var ip))
        {
            CheckIpAddress(ip, allowLoopback);
            if (IsLoopbackAddress(ip))
                return;
        }

        if (scheme != "https")
            throw new G2ACoreException("invalid_arguments", 400, "Remote Hub endpoints MUST use https:// (TLS 1.3)");
    }

    private static bool IsLoopbackAddress(IPAddress ip)
    {
        if (ip.IsIPv4MappedToIPv6) ip = ip.MapToIPv4();
        return IPAddress.IsLoopback(ip);
    }

    private static void CheckIpAddress(IPAddress ip, bool allowLoopback)
    {
        if (ip.IsIPv4MappedToIPv6)
            ip = ip.MapToIPv4();

        if (ip.AddressFamily == AddressFamily.InterNetworkV6)
        {
            byte[] b = ip.GetAddressBytes();
            // 6to4 (2002::/16), NAT64 (64:ff9b::/96 & 64:ff9b:1::/48), Teredo (2001:0000::/32), Doc (2001:db8::/32)
            bool is6to4 = b[0] == 0x20 && b[1] == 0x02;
            bool isNat64 = b[0] == 0x00 && b[1] == 0x64 && b[2] == 0xff && b[3] == 0x9b;
            bool isTeredo = b[0] == 0x20 && b[1] == 0x01 && b[2] == 0x00 && b[3] == 0x00;
            bool isDoc6 = b[0] == 0x20 && b[1] == 0x01 && b[2] == 0x0d && b[3] == 0xb8;
            bool isUla = (b[0] & 0xfe) == 0xfc;
            if (is6to4 || isNat64 || isTeredo || isDoc6 || isUla || ip.IsIPv6LinkLocal || ip.IsIPv6SiteLocal || ip.IsIPv6Multicast || ip.Equals(IPAddress.IPv6None) || ip.Equals(IPAddress.IPv6Any))
                throw new G2ACoreException("permission_denied", 403, $"Non-global or transition-tunnel IPv6 {ip} blocked by SSRF guard");
            if (IPAddress.IsLoopback(ip))
            {
                if (!allowLoopback)
                    throw new G2ACoreException("permission_denied", 403, "Loopback IPv6 blocked by SSRF guard");
                return;
            }
            return;
        }

        byte[] v4 = ip.GetAddressBytes();
        if (v4[0] == 127)
        {
            if (!allowLoopback)
                throw new G2ACoreException("permission_denied", 403, "Loopback IPv4 blocked by SSRF guard");
            return;
        }
        bool isPrivateOrReserved =
            v4[0] == 0 ||
            v4[0] == 10 ||
            (v4[0] == 172 && v4[1] >= 16 && v4[1] <= 31) ||
            (v4[0] == 192 && v4[1] == 168) ||
            (v4[0] == 169 && v4[1] == 254) ||
            (v4[0] == 100 && v4[1] >= 64 && v4[1] <= 127) || // CGNAT 100.64.0.0/10
            (v4[0] == 198 && (v4[1] == 18 || v4[1] == 19)) || // Benchmarking 198.18.0.0/15
            v4[0] >= 224;
        if (isPrivateOrReserved)
            throw new G2ACoreException("permission_denied", 403, $"Non-global IPv4 {ip} blocked by SSRF guard");
    }
}
