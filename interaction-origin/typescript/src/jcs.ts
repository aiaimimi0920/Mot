/**
 * RFC 8785 (JCS) Canonical JSON & Domain-Separated WebCrypto SHA-256 for Browser / WebGL / Node 20+ (G2A V0.0.1).
 */

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
    // JS UTF-16 code-unit comparison matches RFC 8785 Section 3.2.3 directly!
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
