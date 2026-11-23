using System.Globalization;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
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

    private async Task HandleClientAsync(TcpClient client)
    {
        using (client)
        await using (var stream = client.GetStream())
        {
            var (headers, bodyBytes) = await ReadHttpRequestAsync(stream);
            if (headers.Count == 0) return;

            if (!string.IsNullOrEmpty(_sharedToken))
            {
                if (!headers.TryGetValue("authorization", out var auth) || auth != $"Bearer {_sharedToken}")
                {
                    await WriteJsonReplyAsync(stream, 401, "", "not_accepted", null,
                        new Dictionary<string, object?> { ["code"] = "unauthenticated", ["http_status"] = 401, ["message"] = "Invalid Origin token" });
                    return;
                }
            }

            string reqId = "";
            try
            {
                string rawJson = Encoding.UTF8.GetString(bodyBytes);
                using var doc = JcsDigest.ParseStrictDocument(rawJson);
                var root = doc.RootElement;
                reqId = root.TryGetProperty("request_id", out var ridEl) ? ridEl.GetString() ?? "" : "";
                string op = root.TryGetProperty("operation", out var opEl) ? opEl.GetString() ?? "" : "";
                if (op != "origin.execute")
                    throw new G2AOriginException("invalid_request", 400, "Expected origin.execute");

                var payloadEl = root.GetProperty("payload");
                var execReq = ParseExecuteRequest(payloadEl);
                var reply = _sdk.HandleOriginExecute(execReq);
                await WriteJsonReplyAsync(stream, 200, reqId, "accepted", reply, null);
            }
            catch (G2AOriginException gex)
            {
                await WriteJsonReplyAsync(stream, gex.HttpStatus, reqId, "not_accepted", null,
                    new Dictionary<string, object?> { ["code"] = gex.Code, ["http_status"] = gex.HttpStatus, ["message"] = gex.Message });
            }
            catch (Exception ex)
            {
                await WriteJsonReplyAsync(stream, 400, reqId, "not_accepted", null,
                    new Dictionary<string, object?> { ["code"] = "invalid_request", ["http_status"] = 400, ["message"] = ex.Message });
            }
        }
    }

    private static OriginExecuteRequest ParseExecuteRequest(JsonElement p)
    {
        var args = new Dictionary<string, object?>();
        if (p.TryGetProperty("args", out var argsEl) && argsEl.ValueKind == JsonValueKind.Object)
        {
            foreach (var prop in argsEl.EnumerateObject())
                args[prop.Name] = ConvertElement(prop.Value);
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

    private static object? ConvertElement(JsonElement el) => el.ValueKind switch
    {
        JsonValueKind.String => el.GetString(),
        JsonValueKind.Number => el.TryGetInt64(out long l) ? l : el.GetDouble(),
        JsonValueKind.True => true,
        JsonValueKind.False => false,
        JsonValueKind.Null => null,
        _ => el.GetRawText()
    };

    private static async Task<(Dictionary<string, string> Headers, byte[] Body)> ReadHttpRequestAsync(NetworkStream stream)
    {
        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        using var headerBuf = new MemoryStream();
        var one = new byte[1];
        int matchState = 0; // matches \r\n\r\n
        while (await stream.ReadAsync(one.AsMemory(0, 1)) == 1)
        {
            byte b = one[0];
            headerBuf.WriteByte(b);
            if ((matchState == 0 || matchState == 2) && b == (byte)'\r') matchState++;
            else if ((matchState == 1 || matchState == 3) && b == (byte)'\n') matchState++;
            else matchState = b == (byte)'\r' ? 1 : 0;
            if (matchState == 4) break;
        }
        string headerText = Encoding.ASCII.GetString(headerBuf.ToArray());
        string[] lines = headerText.Split("\r\n", StringSplitOptions.RemoveEmptyEntries);
        for (int i = 1; i < lines.Length; i++)
        {
            int idx = lines[i].IndexOf(':');
            if (idx > 0)
                headers[lines[i][..idx].Trim()] = lines[i][(idx + 1)..].Trim();
        }
        int contentLength = headers.TryGetValue("content-length", out var cl) && int.TryParse(cl, NumberStyles.Integer, CultureInfo.InvariantCulture, out int n) ? n : 0;
        var body = new byte[contentLength];
        int offset = 0;
        while (offset < contentLength)
        {
            int read = await stream.ReadAsync(body.AsMemory(offset, contentLength - offset));
            if (read <= 0) break;
            offset += read;
        }
        return (headers, body);
    }

    private static async Task WriteJsonReplyAsync(NetworkStream stream, int statusCode, string reqId, string acceptance, object? result, object? error)
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
        byte[] head = Encoding.ASCII.GetBytes(
            $"HTTP/1.1 {statusCode} {reason}\r\nContent-Type: application/json\r\nContent-Length: {payload.Length}\r\nConnection: close\r\n\r\n");
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

    private readonly OriginSdk _sdk;
    private readonly string _baseUrl;
    private readonly string _slotId;
    private readonly string _sharedToken;
    private readonly HttpClient _http;
    private readonly CancellationTokenSource _cts = new();

    public OutboundExecuteStreamClient(OriginSdk sdk, string hubOriginBaseUrl, string slotId, string sharedToken = "")
    {
        _sdk = sdk;
        _baseUrl = hubOriginBaseUrl.TrimEnd('/');
        _slotId = slotId;
        _sharedToken = sharedToken;
        _http = new HttpClient(new SocketsHttpHandler { UseProxy = false }) { Timeout = Timeout.InfiniteTimeSpan };
        _ = Task.Run(() => StreamLoopAsync(_cts.Token));
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
        using var doc = JcsDigest.ParseStrictDocument(rawJson);
        var root = doc.RootElement;
        string reqId = root.GetProperty("request_id").GetString() ?? "";
        var p = root.GetProperty("payload");
        var args = new Dictionary<string, object?>();
        if (p.TryGetProperty("args", out var argsEl) && argsEl.ValueKind == JsonValueKind.Object)
        {
            foreach (var prop in argsEl.EnumerateObject())
                args[prop.Name] = prop.Value.ValueKind == JsonValueKind.String ? prop.Value.GetString() : prop.Value.GetRawText();
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
        var replyEnv = new Dictionary<string, object?>
        {
            ["g2a_version"] = OriginSdk.WireVersion,
            ["request_id"] = reqId,
            ["timestamp_ms"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            ["acceptance"] = "accepted",
            ["result"] = execRep,
            ["error"] = null
        };
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
        _cts.Dispose();
    }
}
