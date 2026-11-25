using System.Globalization;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace G2A.InteractionOrigin;

/// <summary>
/// HTTP transport client posting Origin Interface envelopes (origin.capabilities, origin.sync, origin.player_event) to Intelligent Hub.
/// </summary>
public sealed class HttpHubTransportClient : IHubTransportClientPort, IDisposable
{
    private readonly string _endpointUrl;
    private readonly string _sharedToken;
    private readonly HttpClient _http;

    public HttpHubTransportClient(string hubOriginBaseUrl, string sharedToken = "")
    {
        _endpointUrl = hubOriginBaseUrl.TrimEnd('/') + "/g2a/v1/origin";
        _sharedToken = sharedToken;
        var handler = new SocketsHttpHandler { UseProxy = false };
        _http = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(8) };
    }

    public static HttpHubTransportClient FromLocalHubDiscovery(string? runtimeFilePath = null, string sharedToken = "")
    {
        string path = runtimeFilePath ?? GetDefaultRuntimeFilePath();
        if (!File.Exists(path))
            throw new G2AOriginException("unavailable", 503, $"Local Hub runtime manifest not found at {path}");
        using var doc = JsonDocument.Parse(File.ReadAllText(path, Encoding.UTF8));
        string originUrl = doc.RootElement.GetProperty("origin_url").GetString()
            ?? throw new G2AOriginException("unavailable", 503, "Missing origin_url in runtime manifest");
        return new HttpHubTransportClient(originUrl, sharedToken);
    }

    public static string GetDefaultRuntimeFilePath()
    {
        string? env = Environment.GetEnvironmentVariable("G2A_HUB_RUNTIME_FILE");
        if (!string.IsNullOrEmpty(env)) return env;
        if (OperatingSystem.IsWindows())
        {
            string localApp = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (!string.IsNullOrEmpty(localApp))
                return Path.Combine(localApp, "G2A", "hub.runtime.json");
        }
        string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        return Path.Combine(home, ".g2a", "hub.runtime.json");
    }

    public void PostToHub(string operation, object payload)
    {
        var envelope = new Dictionary<string, object?>
        {
            ["g2a_version"] = OriginSdk.WireVersion,
            ["request_id"] = "cs-req-" + Guid.NewGuid().ToString("N")[..12],
            ["timestamp_ms"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            ["operation"] = operation,
            ["payload"] = payload,
            ["extensions"] = new Dictionary<string, object?>()
        };
        string json = JsonSerializer.Serialize(envelope);
        using var req = new HttpRequestMessage(HttpMethod.Post, _endpointUrl)
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json")
        };
        if (!string.IsNullOrEmpty(_sharedToken))
            req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _sharedToken);

        using var resp = _http.Send(req);
        string respBody = new StreamReader(resp.Content.ReadAsStream(), Encoding.UTF8).ReadToEnd();
        if (!resp.IsSuccessStatusCode)
        {
            throw new G2AOriginException("unavailable", (int)resp.StatusCode, $"Hub returned {(int)resp.StatusCode}: {respBody}");
        }
    }

    public void Dispose() => _http.Dispose();
}

/// <summary>
/// Unprivileged loopback HTTP/1.1 listener receiving Hub -> Origin "origin.execute" calls across Windows, Linux, macOS, and Unity.
/// </summary>
public sealed class LoopbackExecuteListener : IDisposable
{
    private readonly OriginSdk _sdk;
    private readonly string _sharedToken;
    private readonly TcpListener _listener;
    private readonly CancellationTokenSource _cts = new();

    public string ExecuteUrl { get; }

    public LoopbackExecuteListener(OriginSdk sdk, int port = 0, string sharedToken = "")
    {
        _sdk = sdk;
        _sharedToken = sharedToken;
        _listener = new TcpListener(IPAddress.Loopback, port);
        _listener.Start();
        int boundPort = ((IPEndPoint)_listener.LocalEndpoint).Port;
        ExecuteUrl = $"http://127.0.0.1:{boundPort}/g2a/v1/origin/execute";
        _ = Task.Run(() => AcceptLoopAsync(_cts.Token));
    }

    private async Task AcceptLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            TcpClient client;
            try
            {
                client = await _listener.AcceptTcpClientAsync(ct);
            }
            catch
            {
                break;
            }
            _ = Task.Run(() => HandleClientAsync(client), ct);
        }
    }

    private const int MaxHeaderBytes = 64 * 1024;
    private const int MaxBodyBytes = 4 * 1024 * 1024;

    private async Task HandleClientAsync(TcpClient client)
    {
        using (client)
        await using (var stream = client.GetStream())
        {
            var readBuf = new byte[8192];
            var leftover = new List<byte>();

            while (!_cts.IsCancellationRequested)
            {
                Dictionary<string, string>? headers;
                byte[] bodyBytes;
                try
                {
                    var parsed = await ReadBufferedHttpRequestAsync(stream, readBuf, leftover, _cts.Token);
                    if (parsed is null) return;
                    (headers, bodyBytes) = parsed.Value;
                }
                catch (G2AOriginException gex)
                {
                    await WriteJsonReplyAsync(stream, gex.HttpStatus, "", "not_accepted", null,
                        new Dictionary<string, object?> { ["code"] = gex.Code, ["http_status"] = gex.HttpStatus, ["message"] = gex.Message }, closeConn: true);
                    return;
                }
                catch
                {
                    return;
                }

                bool wantClose = headers.TryGetValue("connection", out var connHdr)
                    && connHdr.Equals("close", StringComparison.OrdinalIgnoreCase);

                if (!string.IsNullOrEmpty(_sharedToken))
                {
                    string expected = $"Bearer {_sharedToken}";
                    if (!headers.TryGetValue("authorization", out var auth) || !SecureEquals(auth, expected))
                    {
                        await WriteJsonReplyAsync(stream, 401, "", "not_accepted", null,
                            new Dictionary<string, object?> { ["code"] = "unauthenticated", ["http_status"] = 401, ["message"] = "Invalid Origin token" }, closeConn: wantClose);
                        if (wantClose) return;
                        continue;
                    }
                }

                string reqId = "";
                try
                {
                    using var doc = JcsDigest.ParseStrictDocument(bodyBytes.AsMemory());
                    var root = doc.RootElement;
                    reqId = root.TryGetProperty("request_id", out var ridEl) ? ridEl.GetString() ?? "" : "";
                    string op = root.TryGetProperty("operation", out var opEl) ? opEl.GetString() ?? "" : "";
                    if (op != "origin.execute")
                        throw new G2AOriginException("invalid_request", 400, "Expected origin.execute");

                    var payloadEl = root.GetProperty("payload");
                    var execReq = ParseExecuteRequest(payloadEl);
                    var reply = _sdk.HandleOriginExecute(execReq);
                    await WriteJsonReplyAsync(stream, 200, reqId, "accepted", reply, null, closeConn: wantClose);
                }
                catch (G2AOriginException gex)
                {
                    await WriteJsonReplyAsync(stream, gex.HttpStatus, reqId, "not_accepted", null,
                        new Dictionary<string, object?> { ["code"] = gex.Code, ["http_status"] = gex.HttpStatus, ["message"] = gex.Message }, closeConn: wantClose);
                }
                catch (Exception ex)
                {
                    await WriteJsonReplyAsync(stream, 400, reqId, "not_accepted", null,
                        new Dictionary<string, object?> { ["code"] = "invalid_request", ["http_status"] = 400, ["message"] = ex.Message }, closeConn: wantClose);
                }

                if (wantClose) return;
            }
        }
    }

    private static bool SecureEquals(string a, string b)
    {
        byte[] ba = Encoding.UTF8.GetBytes(a);
        byte[] bb = Encoding.UTF8.GetBytes(b);
        return ba.Length == bb.Length && CryptographicOperations.FixedTimeEquals(ba, bb);
    }

    private static OriginExecuteRequest ParseExecuteRequest(JsonElement p)
    {
        var args = new Dictionary<string, object?>();
        if (p.TryGetProperty("args", out var argsEl) && argsEl.ValueKind == JsonValueKind.Object)
        {
            foreach (var prop in argsEl.EnumerateObject())
                args[prop.Name] = ConvertJsonElement(prop.Value);
        }
        return new OriginExecuteRequest(
            SlotId: p.GetProperty("slot_id").GetString() ?? "",
            Epoch: p.GetProperty("epoch").GetInt64(),
            Kind: p.GetProperty("kind").GetString() ?? "invoke",
            ExecutionId: p.GetProperty("execution_id").GetString() ?? "",
            MethodId: p.TryGetProperty("method_id", out var m) && m.ValueKind == JsonValueKind.String ? m.GetString() : null,
            Actor: p.TryGetProperty("actor", out var a) && a.ValueKind == JsonValueKind.String ? a.GetString() : null,
            Args: args
        );
    }

    internal static object? ConvertJsonElement(JsonElement el) => el.ValueKind switch
    {
        JsonValueKind.String => el.GetString(),
        JsonValueKind.Number => el.TryGetInt64(out long l) ? l : el.GetDouble(),
        JsonValueKind.True => true,
        JsonValueKind.False => false,
        JsonValueKind.Null => null,
        _ => el.GetRawText()
    };

    private static int FindHeaderEnd(List<byte> buf)
    {
        for (int i = 0; i <= buf.Count - 4; i++)
        {
            if (buf[i] == (byte)'\r' && buf[i + 1] == (byte)'\n' && buf[i + 2] == (byte)'\r' && buf[i + 3] == (byte)'\n')
                return i;
        }
        return -1;
    }

    private static async Task<(Dictionary<string, string> Headers, byte[] Body)?> ReadBufferedHttpRequestAsync(
        NetworkStream stream,
        byte[] readBuf,
        List<byte> leftover,
        CancellationToken ct)
    {
        int headerEndIdx;
        while ((headerEndIdx = FindHeaderEnd(leftover)) < 0)
        {
            if (leftover.Count > MaxHeaderBytes)
                throw new G2AOriginException("invalid_request", 431, "HTTP headers exceed 64 KiB limit");
            int nRead = await stream.ReadAsync(readBuf, ct);
            if (nRead <= 0)
            {
                return null;
            }
            for (int i = 0; i < nRead; i++) leftover.Add(readBuf[i]);
        }

        byte[] headerRaw = new byte[headerEndIdx];
        leftover.CopyTo(0, headerRaw, 0, headerEndIdx);
        leftover.RemoveRange(0, headerEndIdx + 4);

        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        string headerText = Encoding.ASCII.GetString(headerRaw);
        string[] lines = headerText.Split("\r\n", StringSplitOptions.RemoveEmptyEntries);
        if (lines.Length == 0) return null;
        for (int i = 1; i < lines.Length; i++)
        {
            int idx = lines[i].IndexOf(':');
            if (idx > 0)
                headers[lines[i][..idx].Trim()] = lines[i][(idx + 1)..].Trim();
        }

        int contentLength = 0;
        if (headers.TryGetValue("content-length", out var cl))
        {
            if (!int.TryParse(cl, NumberStyles.Integer, CultureInfo.InvariantCulture, out contentLength) || contentLength < 0)
                throw new G2AOriginException("invalid_request", 400, "Invalid Content-Length");
            if (contentLength > MaxBodyBytes)
                throw new G2AOriginException("invalid_request", 413, "Execute payload exceeds 4 MiB limit");
        }

        while (leftover.Count < contentLength)
        {
            int nRead = await stream.ReadAsync(readBuf, ct);
            if (nRead <= 0)
                throw new G2AOriginException("invalid_request", 400, "Truncated HTTP request body");
            for (int i = 0; i < nRead; i++) leftover.Add(readBuf[i]);
        }

        var body = new byte[contentLength];
        if (contentLength > 0)
        {
            leftover.CopyTo(0, body, 0, contentLength);
            leftover.RemoveRange(0, contentLength);
        }
        return (headers, body);
    }

    private static async Task WriteJsonReplyAsync(NetworkStream stream, int statusCode, string reqId, string acceptance, object? result, object? error, bool closeConn = false)
    {
        var env = new Dictionary<string, object?>
        {
            ["g2a_version"] = OriginSdk.WireVersion,
            ["request_id"] = reqId,
            ["timestamp_ms"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            ["acceptance"] = acceptance,
            ["result"] = result,
            ["error"] = error
        };
        byte[] payload = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(env));
        string reason = statusCode == 200 ? "OK" : "Error";
        string connHeader = closeConn ? "close" : "keep-alive";
        byte[] head = Encoding.ASCII.GetBytes(
            $"HTTP/1.1 {statusCode} {reason}\r\nContent-Type: application/json\r\nContent-Length: {payload.Length}\r\nConnection: {connHeader}\r\n\r\n");
        await stream.WriteAsync(head);
        await stream.WriteAsync(payload);
        await stream.FlushAsync();
    }

    public void Dispose()
    {
        _cts.Cancel();
        _listener.Stop();
        _cts.Dispose();
    }
}

/// <summary>
/// Zero-inbound-port Outbound Execute Stream Client ("pull://origin.execute") for console, mobile, or sandboxed C# / Unity games.
/// </summary>
public sealed class OutboundExecuteStreamClient : IDisposable
{
    public const string PullExecuteUrl = "pull://origin.execute";
    public const string PullSentinelUrl = PullExecuteUrl;

    private readonly OriginSdk _sdk;
    private readonly string _baseUrl;
    private readonly string _slotId;
    private readonly string _sharedToken;
    private readonly HttpClient _http;
    private readonly CancellationTokenSource _cts = new();
    private readonly ManualResetEventSlim _connected = new(false);

    public OutboundExecuteStreamClient(OriginSdk sdk, string hubOriginBaseUrl, string slotId, string sharedToken = "")
    {
        _sdk = sdk;
        _baseUrl = hubOriginBaseUrl.TrimEnd('/');
        _slotId = slotId;
        _sharedToken = sharedToken;
        _http = new HttpClient(new SocketsHttpHandler { UseProxy = false }) { Timeout = Timeout.InfiniteTimeSpan };
        _ = Task.Run(() => StreamLoopAsync(_cts.Token));
        _connected.Wait(TimeSpan.FromSeconds(2));
    }

    private async Task StreamLoopAsync(CancellationToken ct)
    {
        string url = $"{_baseUrl}/g2a/v1/origin/execute-stream?slot_id={Uri.EscapeDataString(_slotId)}";
        while (!ct.IsCancellationRequested)
        {
            try
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, url);
                req.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("text/event-stream"));
                if (!string.IsNullOrEmpty(_sharedToken))
                    req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _sharedToken);

                using var resp = await _http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct);
                resp.EnsureSuccessStatusCode();
                _connected.Set();
                await using var stream = await resp.Content.ReadAsStreamAsync(ct);
                using var reader = new StreamReader(stream, Encoding.UTF8);

                var dataLines = new List<string>();
                while (!ct.IsCancellationRequested)
                {
                    string? line = await reader.ReadLineAsync(ct);
                    if (line is null) break;
                    if (line.Length == 0)
                    {
                        if (dataLines.Count > 0)
                        {
                            string joined = string.Join("\n", dataLines);
                            dataLines.Clear();
                            await ProcessStreamedEnvelopeAsync(joined, ct);
                        }
                    }
                    else if (line.StartsWith("data:", StringComparison.Ordinal))
                    {
                        dataLines.Add(line[5..].TrimStart(' '));
                    }
                }
            }
            catch
            {
                if (ct.IsCancellationRequested) break;
                await Task.Delay(150, ct).ConfigureAwait(false);
            }
        }
    }

    private async Task ProcessStreamedEnvelopeAsync(string rawJson, CancellationToken ct)
    {
        string reqId = "";
        Dictionary<string, object?> replyEnv;
        try
        {
            using var doc = JcsDigest.ParseStrictDocument(rawJson);
            var root = doc.RootElement;
            reqId = root.TryGetProperty("request_id", out var ridEl) ? ridEl.GetString() ?? "" : "";
            var p = root.GetProperty("payload");
            var args = new Dictionary<string, object?>();
            if (p.TryGetProperty("args", out var argsEl) && argsEl.ValueKind == JsonValueKind.Object)
            {
                foreach (var prop in argsEl.EnumerateObject())
                    args[prop.Name] = LoopbackExecuteListener.ConvertJsonElement(prop.Value);
            }
            var execReq = new OriginExecuteRequest(
                SlotId: p.GetProperty("slot_id").GetString() ?? "",
                Epoch: p.GetProperty("epoch").GetInt64(),
                Kind: p.GetProperty("kind").GetString() ?? "invoke",
                ExecutionId: p.GetProperty("execution_id").GetString() ?? "",
                MethodId: p.TryGetProperty("method_id", out var m) && m.ValueKind == JsonValueKind.String ? m.GetString() : null,
                Actor: p.TryGetProperty("actor", out var a) && a.ValueKind == JsonValueKind.String ? a.GetString() : null,
                Args: args);

            var execRep = _sdk.HandleOriginExecute(execReq);
            replyEnv = new Dictionary<string, object?>
            {
                ["g2a_version"] = OriginSdk.WireVersion,
                ["request_id"] = reqId,
                ["timestamp_ms"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                ["acceptance"] = "accepted",
                ["result"] = execRep,
                ["error"] = null
            };
        }
        catch (G2AOriginException gex)
        {
            replyEnv = new Dictionary<string, object?>
            {
                ["g2a_version"] = OriginSdk.WireVersion,
                ["request_id"] = reqId,
                ["timestamp_ms"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                ["acceptance"] = "not_accepted",
                ["result"] = null,
                ["error"] = new Dictionary<string, object?> { ["code"] = gex.Code, ["http_status"] = gex.HttpStatus, ["message"] = gex.Message }
            };
        }
        catch (Exception ex)
        {
            replyEnv = new Dictionary<string, object?>
            {
                ["g2a_version"] = OriginSdk.WireVersion,
                ["request_id"] = reqId,
                ["timestamp_ms"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                ["acceptance"] = "not_accepted",
                ["result"] = null,
                ["error"] = new Dictionary<string, object?> { ["code"] = "invalid_request", ["http_status"] = 400, ["message"] = ex.Message }
            };
        }
        using var postReq = new HttpRequestMessage(HttpMethod.Post, $"{_baseUrl}/g2a/v1/origin/execute-reply")
        {
            Content = new StringContent(JsonSerializer.Serialize(replyEnv), Encoding.UTF8, "application/json")
        };
        if (!string.IsNullOrEmpty(_sharedToken))
            postReq.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _sharedToken);
        using var _ = await _http.SendAsync(postReq, ct);
    }

    public void Dispose()
    {
        _cts.Cancel();
        _http.Dispose();
        _connected.Dispose();
        _cts.Dispose();
    }
}
