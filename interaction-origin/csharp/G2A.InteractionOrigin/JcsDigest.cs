using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace G2A.InteractionOrigin;

/// <summary>
/// RFC 8785 (JCS) Canonical JSON Serializer and SHA-256 Domain-Separated Digest Utility for G2A V0.0.1.
/// </summary>
public static class JcsDigest
{
    public static byte[] CanonicalizeUtf8(object? value)
    {
        byte[] rawUtf8 = JsonSerializer.SerializeToUtf8Bytes(value);
        ValidateNoDuplicateKeysOrNonFinite(rawUtf8);
        using var doc = JsonDocument.Parse(rawUtf8, new JsonDocumentOptions
        {
            AllowTrailingCommas = false,
            CommentHandling = JsonCommentHandling.Disallow
        });
        var sb = new StringBuilder(rawUtf8.Length);
        WriteCanonicalElement(sb, doc.RootElement);
        return Encoding.UTF8.GetBytes(sb.ToString());
    }

    public static string ComputeSha256(string domainTag, object? value)
    {
        byte[] canon = CanonicalizeUtf8(value);
        using var hasher = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        hasher.AppendData(Encoding.UTF8.GetBytes(domainTag));
        ReadOnlySpan<byte> sep = stackalloc byte[1] { 0 };
        hasher.AppendData(sep);
        hasher.AppendData(canon);
        Span<byte> digest = stackalloc byte[32];
        hasher.GetHashAndReset(digest);
        return "sha256:" + Convert.ToHexString(digest).ToLowerInvariant();
    }

    public static JsonDocument ParseStrictDocument(string rawJson)
    {
        byte[] utf8 = Encoding.UTF8.GetBytes(rawJson);
        ValidateNoDuplicateKeysOrNonFinite(utf8);
        return JsonDocument.Parse(utf8, new JsonDocumentOptions
        {
            AllowTrailingCommas = false,
            CommentHandling = JsonCommentHandling.Disallow
        });
    }

    private static void ValidateNoDuplicateKeysOrNonFinite(ReadOnlySpan<byte> utf8Bytes)
    {
        var reader = new Utf8JsonReader(utf8Bytes, new JsonReaderOptions
        {
            AllowTrailingCommas = false,
            CommentHandling = JsonCommentHandling.Disallow
        });
        var objectKeyScopes = new Stack<HashSet<string>>();
        while (reader.Read())
        {
            switch (reader.TokenType)
            {
                case JsonTokenType.StartObject:
                    objectKeyScopes.Push(new HashSet<string>(StringComparer.Ordinal));
                    break;
                case JsonTokenType.EndObject:
                    objectKeyScopes.Pop();
                    break;
                case JsonTokenType.PropertyName:
                    string prop = reader.GetString()!;
                    if (!objectKeyScopes.Peek().Add(prop))
                    {
                        throw new InvalidOperationException($"Duplicate JSON object key '{prop}'");
                    }
                    break;
                case JsonTokenType.Number:
                    double d = reader.GetDouble();
                    if (double.IsNaN(d) || double.IsInfinity(d))
                    {
                        throw new InvalidOperationException("Non-finite float is forbidden in strict G2A JSON");
                    }
                    break;
            }
        }
    }

    private static void WriteCanonicalElement(StringBuilder sb, JsonElement el)
    {
        switch (el.ValueKind)
        {
            case JsonValueKind.Null:
            case JsonValueKind.Undefined:
                sb.Append("null");
                break;
            case JsonValueKind.True:
                sb.Append("true");
                break;
            case JsonValueKind.False:
                sb.Append("false");
                break;
            case JsonValueKind.String:
                WriteCanonicalString(sb, el.GetString()!);
                break;
            case JsonValueKind.Number:
                if (el.TryGetInt64(out long i64))
                {
                    sb.Append(i64.ToString(CultureInfo.InvariantCulture));
                }
                else
                {
                    sb.Append(FormatRfc8785Double(el.GetDouble()));
                }
                break;
            case JsonValueKind.Array:
                sb.Append('[');
                bool firstElem = true;
                foreach (var item in el.EnumerateArray())
                {
                    if (!firstElem) sb.Append(',');
                    firstElem = false;
                    WriteCanonicalElement(sb, item);
                }
                sb.Append(']');
                break;
            case JsonValueKind.Object:
                var props = new List<JsonProperty>();
                foreach (var p in el.EnumerateObject()) props.Add(p);
                props.Sort((a, b) => string.CompareOrdinal(a.Name, b.Name)); // Ordinal in .NET compares UTF-16 code units!
                sb.Append('{');
                for (int i = 0; i < props.Count; i++)
                {
                    if (i > 0) sb.Append(',');
                    WriteCanonicalString(sb, props[i].Name);
                    sb.Append(':');
                    WriteCanonicalElement(sb, props[i].Value);
                }
                sb.Append('}');
                break;
        }
    }

    private static void WriteCanonicalString(StringBuilder sb, string s)
    {
        sb.Append('"');
        foreach (char c in s)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\b': sb.Append("\\b"); break;
                case '\f': sb.Append("\\f"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c <= 0x1F)
                        sb.AppendFormat(CultureInfo.InvariantCulture, "\\u{0:x4}", (int)c);
                    else
                        sb.Append(c);
                    break;
            }
        }
        sb.Append('"');
    }

    private static string FormatRfc8785Double(double f)
    {
        if (double.IsNaN(f) || double.IsInfinity(f))
            throw new InvalidOperationException("Non-finite float is forbidden");
        if (f == 0.0) return "0";
        double abs = Math.Abs(f);
        if (abs >= 1e-6 && abs < 1e21)
        {
            if (Math.Truncate(f) == f && abs <= 9007199254740991d)
                return ((long)f).ToString(CultureInfo.InvariantCulture);
            string s = f.ToString("G17", CultureInfo.InvariantCulture);
            if (!s.Contains('E') && !s.Contains('e'))
                return s.Contains('.') ? s.TrimEnd('0').TrimEnd('.') : s;
            return f.ToString("0.################", CultureInfo.InvariantCulture);
        }
        string exp = f.ToString("e16", CultureInfo.InvariantCulture);
        int eIdx = exp.IndexOf('e');
        string mant = exp[..eIdx].TrimEnd('0').TrimEnd('.');
        int expVal = int.Parse(exp[(eIdx + 1)..], CultureInfo.InvariantCulture);
        return expVal >= 0
            ? string.Create(CultureInfo.InvariantCulture, $"{mant}e+{expVal}")
            : string.Create(CultureInfo.InvariantCulture, $"{mant}e{expVal}");
    }
}
