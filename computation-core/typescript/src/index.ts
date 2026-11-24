export const G2A_WIRE_VERSION = "0.0.1";
export const MAX_SEEN_EVENT_IDS = 1024;

/**
 * Parses UTF-8 JSON text while strictly rejecting duplicate object keys at any nesting depth (RFC 8259 / I-JSON).
 */
export function parseStrictJson<T = unknown>(raw: string): T {
  let pos = 0;
  const n = raw.length;
  const skipWs = () => {
    while (pos < n && (raw[pos] === " " || raw[pos] === "\t" || raw[pos] === "\r" || raw[pos] === "\n")) pos++;
  };
  const scanString = (): string => {
    const start = pos;
    pos++; // '"'
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
    if (!Number.isFinite(val)) throw new Error("G2A strict JSON forbids NaN/Infinity");
    return Object.is(val, -0) ? "0" : JSON.stringify(val);
  }
  if (typeof val === "string") return JSON.stringify(val);
  if (Array.isArray(val)) return "[" + val.map(canonicalizeJcs).join(",") + "]";
  if (typeof val === "object") {
    const o = val as Record<string, unknown>;
    const keys = Object.keys(o)
      .filter((k) => o[k] !== undefined)
      .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
    return "{" + keys.map((k) => `${JSON.stringify(k)}:${canonicalizeJcs(o[k])}`).join(",") + "}";
  }
  throw new Error(`Unsupported JCS type: ${typeof val}`);
}

export async function jcsSha256(domainTag: string, val: unknown): Promise<string> {
  const enc = new TextEncoder();
  const prefix = enc.encode(domainTag + "\0");
  const body = enc.encode(canonicalizeJcs(val));
  const merged = new Uint8Array(prefix.length + body.length);
  merged.set(prefix, 0);
  merged.set(body, prefix.length);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", merged);
  const hex = Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return `sha256:${hex}`;
}

export function validateOutboundUrlAgainstSsrf(url: string, allowLoopback = false): void {
  const parsed = new URL(url);
  if (parsed.username || parsed.password) {
    throw new G2ACoreError("invalid_arguments", 400, "Credentials MUST NOT be embedded in endpoint URLs");
  }
  const host = parsed.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  const normalized = host.startsWith("::ffff:") ? host.slice(7) : host;
  const isLoopback = normalized === "127.0.0.1" || normalized === "localhost" || normalized === "::1" || normalized.startsWith("127.");
  if (isLoopback) {
    if (!allowLoopback) {
      throw new G2ACoreError("permission_denied", 403, "Loopback Hub address blocked by remote SSRF policy");
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new G2ACoreError("invalid_arguments", 400, "Unsupported URL scheme");
    }
    return;
  }
  const isPrivateOrLinkLocal =
    normalized === "0.0.0.0" ||
    normalized.startsWith("10.") ||
    normalized.startsWith("192.168.") ||
    normalized.startsWith("169.254.") ||
    normalized.startsWith("fe80:") ||
    normalized.startsWith("fc") ||
    normalized.startsWith("fd") ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(normalized);
  if (isPrivateOrLinkLocal) {
    throw new G2ACoreError("permission_denied", 403, `Private or link-local host ${host} blocked by SSRF guard`);
  }
  if (parsed.protocol !== "https:") {
    throw new G2ACoreError("invalid_arguments", 400, "Remote Hub endpoints MUST use https:// (TLS 1.3)");
  }
}

export interface QueueStep {
  step_index: number;
  method_id: string;
  actor: string;
  args: Record<string, unknown>;
}

export interface MethodCallQueue {
  queue_id: string;
  epoch: number;
  based_on_rev: number;
  trigger: "player_command" | "proactive";
  supersedes: boolean;
  intent_summary: string;
  steps: QueueStep[];
}

export interface ProjectedContext {
  slot_id: string;
  epoch: number;
  rev: number;
  capability_table_digest: string;
  player_command?: string;
  state_brief: Record<string, unknown>;
  active_methods: Array<{
    method_id: string;
    actor: string;
    readiness: "ready" | "staged";
    targets: Array<{
      target_id: string;
      label: string;
      readiness: "ready" | "staged";
      actor_anchor?: string;
      player_anchor?: string;
      alert_tag?: string;
    }>;
  }>;
  visual_ref?: Record<string, unknown>;
  audio_ref?: Record<string, unknown>;
  allowed_timeline_tags?: string[];
}

export interface PlanDecision {
  intent_summary: string;
  planned_steps?: QueueStep[];
  reply_text?: string;
  audience?: "player_only" | "party" | "public";
}

export interface MemoryRecordV1 {
  memory_id: string;
  created_at_ms: number;
  imported_at_ms?: number | null;
  epistemic_source: "game_observed" | "player_stated" | "agent_summary" | "player_edited";
  game_origin_id?: string | null;
  timeline_tag?: string | null;
  pinned_by_player: boolean;
  max_disclosure_scope: "private_player_only" | "same_game_companion" | "cross_game_companion";
  content: string;
  observed_epoch?: number;
}

export interface MemoryBundleV1 {
  format: "g2a.memory.bundle.v1";
  exported_at_ms: number;
  persona_settings: Record<string, unknown>;
  autonomy_preferences: Record<string, string>;
  records: MemoryRecordV1[];
}

export type CognitivePlanner = (
  capabilities: unknown[],
  context: ProjectedContext
) => Promise<PlanDecision> | PlanDecision;

export class G2ACoreError extends Error {
  constructor(
    public readonly code: string,
    public readonly httpStatus: number,
    message: string,
    public readonly retryAfterMs?: number,
    public readonly cooldownMs?: number,
    public readonly freshContext?: ProjectedContext
  ) {
    super(`${code} (${httpStatus}): ${message}`);
  }
}

/**
 * Tier 3 Computation Core SDK for TypeScript / Node 20+ / Edge AI Agents (G2A V0.0.1).
 * Implements capability-digest caching, WebSocket/SSE stream recovery with session.resume & replay=1,
 * bounded deduplication, proactive refusal cooldown shield, confirmation queue_id preservation,
 * and mandatory 504 outcome_unknown -> queue.query reconciliation.
 */
export class CoreSDK {
  private capabilityCache = new Map<string, unknown[]>();
  private slotDigests = new Map<string, string>();
  private pendingConfirmQueues = new Map<string, MethodCallQueue>();
  private refusalCooldowns = new Map<string, number>();
  private seenEvents = new Set<string>();
  private ws: WebSocket | null = null;
  private sse: EventSource | null = null;
  private hbTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  public sessionId: string | null = null;
  public actionsPaused = false;
  public closed = false;

  constructor(
    private readonly planner: CognitivePlanner,
    private readonly allowLoopback = false,
    private readonly streamMode: "websocket" | "sse" = "websocket"
  ) {}

  async connect(
    hubEndpoint: string,
    authToken: string,
    slotId: string,
    options?: { playerId?: string; factionId?: string; requestedScope?: Record<string, unknown> }
  ): Promise<void> {
    validateOutboundUrlAgainstSsrf(hubEndpoint, this.allowLoopback);

    const base = hubEndpoint.replace(/\/$/, "");
    const cachedDigest = this.slotDigests.get(slotId);
    const res = await this.postEnvelope(base, authToken, "session.authorize", {
      slot_id: slotId,
      player_id: options?.playerId,
      faction_id: options?.factionId,
      requested_scope: options?.requestedScope ?? { allow_chat: true },
      cached_capability_digest: cachedDigest,
    });

    const capDigest = String(res.capability_table_digest);
    if (Array.isArray(res.capability_table)) {
      const verified = await jcsSha256("g2a.cap.v1", res.capability_table);
      if (verified !== capDigest) {
        throw new G2ACoreError("invalid_arguments", 400, "Mismatched capability_table_digest");
      }
      this.capabilityCache.set(capDigest, res.capability_table);
    }
    this.slotDigests.set(slotId, capDigest);
    this.sessionId = String(res.session_id);
    this.closed = false;

    this.hbTimer = setInterval(async () => {
      if (this.closed || !this.sessionId) return;
      try {
        await this.postEnvelope(base, authToken, "session.heartbeat", {}, this.sessionId);
      } catch (err) {
        if (err instanceof G2ACoreError && (err.code === "revoked" || err.code === "permission_denied")) {
          this.shutdown();
        }
      }
    }, 5000);

    if (this.streamMode === "sse") {
      this.openSseStream(base, authToken, false);
    } else {
      this.openWebSocketStream(base, authToken, false);
    }
  }

  shutdown(): void {
    this.closed = true;
    if (this.hbTimer) clearInterval(this.hbTimer);
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
    this.sse?.close();
    this.sse = null;
  }

  async onContextReceived(base: string, authToken: string, ctx: ProjectedContext, depth = 0): Promise<void> {
    if (this.closed || this.actionsPaused || !this.sessionId) return;
    const caps = this.capabilityCache.get(ctx.capability_table_digest) ?? [];
    const decision = await this.planner(caps, ctx);

    if (decision.reply_text) {
      await this.postEnvelope(base, authToken, "chat.send", {
        message_id: `msg-${crypto.randomUUID().slice(0, 12)}`,
        audience: decision.audience ?? "player_only",
        text: decision.reply_text,
        provenance: "ai_companion",
      }, this.sessionId);
    }

    if (decision.planned_steps && decision.planned_steps.length > 0) {
      const isCmd = ctx.player_command !== undefined && ctx.player_command !== null;
      const queue: MethodCallQueue = {
        queue_id: `Q-${crypto.randomUUID().slice(0, 12)}`,
        epoch: ctx.epoch,
        based_on_rev: ctx.rev,
        trigger: isCmd ? "player_command" : "proactive",
        supersedes: isCmd,
        intent_summary: decision.intent_summary,
        steps: decision.planned_steps,
      };
      await this.submitQueueWithRecovery(base, authToken, queue, depth);
    }
  }

  async submitQueueWithRecovery(base: string, authToken: string, queue: MethodCallQueue, depth = 0): Promise<unknown> {
    if (this.closed || !this.sessionId) return null;
    const now = Date.now();
    for (const [fpKey, until] of this.refusalCooldowns.entries()) {
      if (now >= until) this.refusalCooldowns.delete(fpKey);
    }
    if (queue.trigger === "proactive") {
      const fp = await jcsSha256("g2a.fp.v1", queue.steps);
      if (now < (this.refusalCooldowns.get(fp) ?? 0)) {
        return null;
      }
    }

    try {
      return await this.postEnvelope(base, authToken, "queue.submit", queue, this.sessionId);
    } catch (err) {
      if (!(err instanceof G2ACoreError)) throw err;
      if (err.code === "confirmation_required") {
        this.pendingConfirmQueues.set(queue.queue_id, queue);
        return null;
      }
      if (err.code === "stale_context" && err.freshContext && depth < 2) {
        return this.onContextReceived(base, authToken, err.freshContext, depth + 1);
      }
      if (err.code === "outcome_unknown") {
        return this.postEnvelope(base, authToken, "queue.query", { queue_id: queue.queue_id }, this.sessionId);
      }
      return null;
    }
  }

  private async handlePushEnvelope(base: string, authToken: string, env: Record<string, any>): Promise<void> {
    if (typeof env.event_id === "string" && env.event_id) {
      if (this.seenEvents.has(env.event_id)) return;
      this.seenEvents.add(env.event_id);
      if (this.seenEvents.size > MAX_SEEN_EVENT_IDS) {
        const oldest = this.seenEvents.values().next().value;
        if (oldest !== undefined) this.seenEvents.delete(oldest);
      }
    }
    if (env.event === "context.push") {
      await this.onContextReceived(base, authToken, env.payload as ProjectedContext);
    } else if (env.event === "queue.confirmed") {
      const q = this.pendingConfirmQueues.get(env.payload?.queue_id);
      if (q) {
        this.pendingConfirmQueues.delete(q.queue_id);
        await this.submitQueueWithRecovery(base, authToken, q);
      }
    } else if (env.event === "queue.declined") {
      const q = this.pendingConfirmQueues.get(env.payload?.queue_id);
      if (q) {
        this.pendingConfirmQueues.delete(q.queue_id);
        const fp = await jcsSha256("g2a.fp.v1", q.steps);
        const cdMs = Number(env.payload?.cooldown_ms ?? 60000);
        this.refusalCooldowns.set(fp, Date.now() + cdMs);
      }
    } else if (env.event === "session.resumed") {
      this.actionsPaused = false;
      await this.onContextReceived(base, authToken, env.payload as ProjectedContext);
    } else if (env.event === "session.closed") {
      this.shutdown();
    }
  }

  private async resumeBeforeReconnect(base: string, authToken: string): Promise<boolean> {
    if (this.closed || !this.sessionId) return false;
    try {
      const res = await this.postEnvelope(base, authToken, "session.resume", {}, this.sessionId);
      this.actionsPaused = Boolean(res.actions_paused);
      return true;
    } catch (err) {
      if (err instanceof G2ACoreError && (err.code === "revoked" || err.code === "permission_denied")) {
        this.shutdown();
        return false;
      }
      return true;
    }
  }

  private openWebSocketStream(base: string, authToken: string, isReconnect: boolean): void {
    if (typeof WebSocket === "undefined" || this.closed || !this.sessionId) return;
    const replay = isReconnect ? "&replay=1" : "";
    const wsUrl =
      base.replace(/^http/, "ws") +
      `/g2a/v1/core/ws?session_id=${encodeURIComponent(this.sessionId)}&access_token=${encodeURIComponent(authToken)}${replay}`;
    this.ws = new WebSocket(wsUrl);
    this.ws.onmessage = async (ev: MessageEvent) => {
      const env = parseStrictJson<Record<string, any>>(String(ev.data));
      await this.handlePushEnvelope(base, authToken, env);
    };
    this.ws.onclose = () => {
      if (this.closed) return;
      this.reconnectTimer = setTimeout(async () => {
        if (await this.resumeBeforeReconnect(base, authToken)) {
          this.openWebSocketStream(base, authToken, true);
        }
      }, 250);
    };
  }

  private openSseStream(base: string, authToken: string, isReconnect: boolean): void {
    if (typeof EventSource === "undefined" || this.closed || !this.sessionId) return;
    const replay = isReconnect ? "&replay=1" : "";
    const sseUrl =
      `${base}/g2a/v1/core/events?session_id=${encodeURIComponent(this.sessionId)}&access_token=${encodeURIComponent(authToken)}${replay}`;
    this.sse = new EventSource(sseUrl);
    for (const evType of ["context.push", "queue.confirmed", "queue.declined", "session.resumed", "session.closed"]) {
      this.sse.addEventListener(evType, async (ev: MessageEvent) => {
        const env = parseStrictJson<Record<string, any>>(String(ev.data));
        await this.handlePushEnvelope(base, authToken, env);
      });
    }
  }

  private async postEnvelope(
    base: string,
    authToken: string,
    operation: string,
    payload: unknown,
    sessionId?: string
  ): Promise<Record<string, unknown>> {
    const resp = await fetch(`${base}/g2a/v1/core`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${authToken}`,
      },
      body: JSON.stringify({
        g2a_version: G2A_WIRE_VERSION,
        request_id: `req-${crypto.randomUUID().slice(0, 12)}`,
        session_id: sessionId,
        timestamp_ms: Date.now(),
        operation,
        payload,
        extensions: {},
      }),
    });
    const raw = await resp.text();
    const env = parseStrictJson<Record<string, any>>(raw);
    if (env.acceptance === "accepted") return (env.result ?? {}) as Record<string, unknown>;
    const e = env.error ?? { code: "invalid_request", http_status: resp.status, message: "Rejected" };
    throw new G2ACoreError(e.code, e.http_status, e.message, e.retry_after_ms, e.cooldown_ms, e.fresh_context);
  }
}
