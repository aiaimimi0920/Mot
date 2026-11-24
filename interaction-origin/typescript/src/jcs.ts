/**
 * RFC 8785 (JCS) Canonical JSON, Strict RFC 8259 Duplicate-Key Validator,
 * & Domain-Separated WebCrypto SHA-256 for Browser / WebGL / Node 20+ (G2A V0.0.1).
 */

export function parseStrictJson<T = unknown>(raw: string): T {
  let pos = 0;
  const n = raw.length;
  const skipWs = () => {
    while (pos < n && (raw[pos] === " " || raw[pos] === "\t" || raw[pos] === "\r" || raw[pos] === "\n")) pos++;
  };
  const scanString = (): string => {
    const start = pos;
    pos++;
    while (pos < n) {
      const ch = raw[pos++];
      if (ch === "\\") {
        pos += raw[pos] === "u" ? 5 : 1;
      } else if (ch === '"') {
        return JSON.parse(raw.slice(start, pos)) as string;
      }
    }
    throw new Error("Unterminated JSON string");
  };
  const scanValue = (): void => {
    skipWs();
    if (pos >= n) throw new Error("Unexpected end of JSON");
    const c = raw[pos];
    if (c === '"') {
      scanString();
    } else if (c === "{") {
      pos++;
      skipWs();
      if (raw[pos] === "}") {
        pos++;
        return;
      }
      const seen = new Set<string>();
      while (pos < n) {
        skipWs();
        if (raw[pos] !== '"') throw new Error("Expected string key in JSON object");
        const key = scanString();
        if (seen.has(key)) throw new Error(`Duplicate JSON object key: ${key}`);
        seen.add(key);
        skipWs();
        if (raw[pos++] !== ":") throw new Error("Expected ':' after JSON key");
        scanValue();
        skipWs();
        const sep = raw[pos++];
        if (sep === "}") break;
        if (sep !== ",") throw new Error("Expected ',' or '}' in JSON object");
      }
    } else if (c === "[") {
      pos++;
      skipWs();
      if (raw[pos] === "]") {
        pos++;
        return;
      }
      while (pos < n) {
        scanValue();
        skipWs();
        const sep = raw[pos++];
        if (sep === "]") break;
        if (sep !== ",") throw new Error("Expected ',' or ']' in JSON array");
      }
    } else {
      while (pos < n && !",]} \t\r\n".includes(raw[pos])) pos++;
    }
  };
  scanValue();
  skipWs();
  if (pos !== n) throw new Error("Trailing characters in strict JSON");
  return JSON.parse(raw) as T;
}

export function canonicalizeJcs(val: unknown): string {
  if (val === null || val === undefined) return "null";
  if (typeof val === "boolean") return val ? "true" : "false";
  if (typeof val === "number") {
    if (!Number.isFinite(val)) {
      throw new Error("G2A strict JSON forbids NaN or Infinity");
    }
    return Object.is(val, -0) ? "0" : JSON.stringify(val); // ECMAScript Number::toString IS the normative reference of RFC 8785!
  }
  if (typeof val === "string") {
    return JSON.stringify(val);
  }
  if (Array.isArray(val)) {
    return "[" + val.map((item) => canonicalizeJcs(item)).join(",") + "]";
  }
  if (typeof val === "object") {
    const obj = val as Record<string, unknown>;
    const keys = Object.keys(obj)
      .filter((k) => obj[k] !== undefined)
      .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
    return "{" + keys.map((k) => `${JSON.stringify(k)}:${canonicalizeJcs(obj[k])}`).join(",") + "}";
  }
  throw new Error(`Unsupported JCS value type: ${typeof val}`);
}

export async function jcsSha256(domainTag: string, val: unknown): Promise<string> {
  const enc = new TextEncoder();
  const prefix = enc.encode(domainTag + "\0");
  const body = enc.encode(canonicalizeJcs(val));
  const merged = new Uint8Array(prefix.length + body.length);
  merged.set(prefix, 0);
  merged.set(body, prefix.length);
  const hashBuf = await globalThis.crypto.subtle.digest("SHA-256", merged);
  const hex = Array.from(new Uint8Array(hashBuf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return `sha256:${hex}`;
}
