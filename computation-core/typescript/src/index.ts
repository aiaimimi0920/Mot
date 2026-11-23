export const G2A_WIRE_VERSION = "0.0.1";

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
 * Implements capability-digest caching, WebSocket/SSE stream recovery, confirmation queue_id preservation,
 * and mandatory 504 outcome_unknown -> queue.query reconciliation.
 */
export class CoreSDK {
  private capabilityCache = new Map<string, unknown[]>();
  private slotDigests = new Map<string, string>();
  private pendingConfirmQueues = new Map<string, MethodCallQueue>();
  private seenEvents = new Set<string>();
  private ws: WebSocket | null = null;
  private hbTimer: ReturnType<typeof setInterval> | null = null;

  public sessionId: string | null = null;
  public actionsPaused = false;
  public closed = false;

  constructor(
    private readonly planner: CognitivePlanner,
    private readonly allowLoopback = false
  ) {}

  async connect(
    hubEndpoint: string,
    authToken: string,
    slotId: string,
    options?: { playerId?: string; factionId?: string; requestedScope?: Record<string, unknown> }
  ): Promise<void> {
    const parsed = new URL(hubEndpoint);
    const isLocal = parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost";
    if (!isLocal && parsed.protocol !== "https:") {
      throw new G2ACoreError("invalid_arguments", 400, "Remote Hub endpoints MUST use https:// (TLS 1.3)");
    }
    if (isLocal && !this.allowLoopback && parsed.protocol !== "https:") {
      throw new G2ACoreError("permission_denied", 403, "Loopback HTTP blocked unless allowLoopback=true");
    }

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

    this.openWebSocketStream(base, authToken);
  }

  shutdown(): void {
    this.closed = true;
    if (this.hbTimer) clearInterval(this.hbTimer);
    this.ws?.close();
    this.ws = null;
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

  private openWebSocketStream(base: string, authToken: string): void {
    if (typeof WebSocket === "undefined" || !this.sessionId) return;
    const wsUrl = base.replace(/^http/, "ws") + `/g2a/v1/core/ws?session_id=${encodeURIComponent(this.sessionId)}`;
    this.ws = new WebSocket(wsUrl);
    this.ws.onmessage = async (ev: MessageEvent) => {
      const env = JSON.parse(String(ev.data));
      if (env.event_id) {
        if (this.seenEvents.has(env.event_id)) return;
        this.seenEvents.add(env.event_id);
      }
      if (env.event === "context.push") {
        await this.onContextReceived(base, authToken, env.payload as ProjectedContext);
      } else if (env.event === "queue.confirmed") {
        const q = this.pendingConfirmQueues.get(env.payload.queue_id);
        if (q) {
          this.pendingConfirmQueues.delete(q.queue_id);
          await this.submitQueueWithRecovery(base, authToken, q);
        }
      } else if (env.event === "session.resumed") {
        this.actionsPaused = false;
        await this.onContextReceived(base, authToken, env.payload as ProjectedContext);
      } else if (env.event === "session.closed") {
        this.shutdown();
      }
    };
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
    const env = await resp.json();
    if (env.acceptance === "accepted") return (env.result ?? {}) as Record<string, unknown>;
    const e = env.error ?? { code: "invalid_request", http_status: resp.status, message: "Rejected" };
    throw new G2ACoreError(e.code, e.http_status, e.message, e.retry_after_ms, e.cooldown_ms, e.fresh_context);
  }
}
