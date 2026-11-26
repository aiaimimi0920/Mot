import { canonicalizeJcs, jcsSha256, parseStrictJson } from "./jcs.js";

export const G2A_WIRE_VERSION = "0.0.1";
export const MAX_CANDIDATE_TARGETS_PER_METHOD = 24;
export const MAX_EXECUTION_LEDGER_ENTRIES = 512;

export type PolicyMode = "allow" | "confirm" | "prohibit";
export type Readiness = "ready" | "staged";

export interface Pose3D {
  x: number;
  y: number;
  z: number;
  yaw_deg?: number;
}

export function formatPlayerDeicticAnchor(player: Pose3D, target: Pose3D, isPointed = false): string {
  const dx = target.x - player.x;
  const dy = target.y - player.y;
  const dz = target.z - player.z;
  const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
  const distStr = `${dist.toFixed(1)}m`;
  if (isPointed) return `pointed_ray ${distStr}`;
  if (dx * dx + dz * dz < 1e-6) return `here ${distStr}`;
  const worldDeg = Math.atan2(dx, dz) * (180 / Math.PI);
  let rel = (worldDeg - (player.yaw_deg ?? 0)) % 360;
  if (rel > 180) rel -= 360;
  else if (rel <= -180) rel += 360;

  let sector = "behind";
  if (rel >= -22.5 && rel <= 22.5) sector = "ahead";
  else if (rel > 22.5 && rel <= 67.5) sector = "front-right";
  else if (rel > 67.5 && rel <= 112.5) sector = "right";
  else if (rel > 112.5 && rel <= 157.5) sector = "back-right";
  else if (rel >= -67.5 && rel < -22.5) sector = "front-left";
  else if (rel >= -112.5 && rel < -67.5) sector = "left";
  else if (rel >= -157.5 && rel < -112.5) sector = "back-left";
  return `${sector} ${distStr}`;
}

export function validateArgsAgainstSchema(
  args: Record<string, unknown>,
  schema?: Record<string, unknown>,
  prefix = "",
  depth = 0
): string | null {
  if (depth > 16) return "schema_depth_exceeded";
  if (!schema || Object.keys(schema).length === 0) return null;
  if (Array.isArray(schema.required)) {
    for (const f of schema.required) {
      if (typeof f === "string" && !(f in args)) {
        return `missing_required_arg:${prefix ? `${prefix}.${f}` : f}`;
      }
    }
  }
  const props = schema.properties as Record<string, Record<string, unknown>> | undefined;
  if (props && typeof props === "object") {
    if (schema.additionalProperties === false) {
      for (const k of Object.keys(args)) {
        if (!(k in props)) return `unexpected_arg:${prefix ? `${prefix}.${k}` : k}`;
      }
    }
    for (const [k, v] of Object.entries(args)) {
      if (props[k]) {
        const qual = prefix ? `${prefix}.${k}` : k;
        const err = validateFieldSchema(qual, v, props[k], depth + 1);
        if (err) return err;
      }
    }
  }
  return null;
}

function validateFieldSchema(name: string, val: unknown, spec: Record<string, unknown>, depth: number): string | null {
  if (depth > 16) return "schema_depth_exceeded";
  const ptype = spec.type;
  if (ptype === "string") {
    if (typeof val !== "string") return `invalid_type:${name}`;
    const len = [...val].length;
    if (typeof spec.minLength === "number" && len < spec.minLength) return `min_length:${name}`;
    if (typeof spec.maxLength === "number" && len > spec.maxLength) return `max_length:${name}`;
    if (Array.isArray(spec.enum) && !spec.enum.includes(val)) return `invalid_enum:${name}`;
    return null;
  }
  if (ptype === "boolean") return typeof val === "boolean" ? null : `invalid_type:${name}`;
  if (ptype === "integer") {
    if (typeof val !== "number" || !Number.isInteger(val)) return `invalid_type:${name}`;
    return checkNumBounds(name, val, spec);
  }
  if (ptype === "number") {
    if (typeof val !== "number" || !Number.isFinite(val)) return `invalid_type:${name}`;
    return checkNumBounds(name, val, spec);
  }
  if (ptype === "array") {
    if (!Array.isArray(val)) return `invalid_type:${name}`;
    if (typeof spec.minItems === "number" && val.length < spec.minItems) return `min_items:${name}`;
    if (typeof spec.maxItems === "number" && val.length > spec.maxItems) return `max_items:${name}`;
    if (spec.items && typeof spec.items === "object") {
      for (let i = 0; i < val.length; i++) {
        const err = validateFieldSchema(`${name}[${i}]`, val[i], spec.items as Record<string, unknown>, depth + 1);
        if (err) return err;
      }
    }
    return null;
  }
  if (ptype === "object") {
    if (!val || typeof val !== "object" || Array.isArray(val)) return `invalid_type:${name}`;
    return validateArgsAgainstSchema(val as Record<string, unknown>, spec, name, depth + 1);
  }
  return null;
}

function checkNumBounds(name: string, v: number, spec: Record<string, unknown>): string | null {
  if (typeof spec.minimum === "number" && v < spec.minimum) return `below_minimum:${name}`;
  if (typeof spec.maximum === "number" && v > spec.maximum) return `above_maximum:${name}`;
  if (typeof spec.exclusiveMinimum === "number" && v <= spec.exclusiveMinimum) return `below_exclusive_minimum:${name}`;
  if (typeof spec.exclusiveMaximum === "number" && v >= spec.exclusiveMaximum) return `above_exclusive_maximum:${name}`;
  return null;
}

export async function discoverLocalHubOriginUrl(explicitPath?: string): Promise<string> {
  const fsMod = await import("node:fs/promises");
  const pathMod = await import("node:path");
  const osMod = await import("node:os");
  let p = explicitPath || process.env.G2A_HUB_RUNTIME_FILE || "";
  if (!p) {
    if (process.platform === "win32" && process.env.LOCALAPPDATA) {
      p = pathMod.join(process.env.LOCALAPPDATA, "G2A", "hub.runtime.json");
    } else {
      p = pathMod.join(osMod.homedir(), ".g2a", "hub.runtime.json");
    }
  }
  const raw = await fsMod.readFile(p, "utf8");
  const doc = parseStrictJson<Record<string, any>>(raw);
  if (typeof doc.origin_url !== "string" || !doc.origin_url) {
    throw new Error("Missing origin_url in Hub runtime manifest");
  }
  return doc.origin_url.replace(/\/$/, "");
}

export interface CapabilityEntry {
  method_id: string;
  name: string;
  description: string;
  permitted_actors: string[];
  params_schema: Record<string, unknown>;
  sustained?: boolean;
  risk_flags?: string[];
  default_policy?: PolicyMode;
}

export interface TargetCandidate {
  target_id: string;
  label: string;
  readiness: Readiness;
  prerequisite_hint?: string;
  actor_anchor?: string;
  player_anchor?: string;
  alert_tag?: string;
}

export interface ActiveMethodGrant {
  method_id: string;
  actor: string;
  readiness: Readiness;
  targets: TargetCandidate[];
}

export interface OriginExecuteRequest {
  slot_id: string;
  epoch: number;
  kind: "invoke" | "cancel" | "deliver_chat";
  execution_id: string;
  method_id?: string;
  actor?: string;
  args: Record<string, unknown>;
}

export interface OriginExecuteReply {
  execution_id: string;
  state: "executing" | "succeeded" | "failed" | "cancelled";
  effect_summary?: Record<string, unknown>;
  failure_reason?: string;
}

export type EffectInvocationResult =
  | { kind: "succeeded"; effect_summary?: Record<string, unknown> }
  | { kind: "failed"; reason: string }
  | { kind: "executing"; cancel_fn: () => void };

export type DomainEffectHandler = (
  actor: string,
  subject: unknown,
  args: Record<string, unknown>,
  executionId: string
) => EffectInvocationResult;

export interface SlotPublishOptions {
  disabled?: boolean;
  max_sessions?: number;
  shared_cooldown_ms?: number;
  allowlisted_cores?: string[];
  allowlist_criteria_url?: string;
}

export interface QuickOriginConfig {
  slotId: string;
  hubOriginUrl?: string; // "auto" or explicit http://...
  fallbackUrl?: string;
  roleKind?: "companion" | "independent_npc";
  billingMode?: "player_metered" | "developer_sponsored";
  originToken?: string;
  roomId?: string;
  factionId?: string;
  actors?: string[];
  onChatDelivered?: (audience: string, text: string) => void;
  setup?: (sdk: OriginSDK) => void | Promise<void>;
  publishOptions?: SlotPublishOptions;
}

/**
 * Tier 1 Interaction Origin SDK for Browser / HTML5 / Three.js / Tauri / Node 20+ games (G2A V0.0.1).
 * Uses zero-inbound-port streaming ("pull://origin.execute") over EventSource or universal fetch ReadableStream.
 */
export class OriginSDK {
  private capabilities = new Map<string, { def: Required<CapabilityEntry>; handler: DomainEffectHandler }>();
  private actors = new Map<string, unknown>();
  private modules = new Map<string, { snapshotFn: () => Record<string, unknown>; grantsFn: () => ActiveMethodGrant[] }>();
  private inFlight = new Map<string, () => void>();
  private ledger = new Map<string, OriginExecuteReply>();
  private pendingSettled: OriginExecuteReply[] = [];
  private lastSentMethods = new Map<string, string>();
  private lastBriefDigest = "";
  private extensionsData: Record<string, Record<string, unknown>> = {};
  private eventSource: EventSource | null = null;
  private streamAbort: AbortController | null = null;

  public currentEpoch = 1;
  public currentRev = 0;
  public syncEnabled = true;

  constructor(
    public readonly slotId: string,
    public readonly hubOriginBaseUrl: string = "http://127.0.0.1:19210",
    public readonly roleKind: "companion" | "independent_npc" = "companion",
    public readonly billingMode: "player_metered" | "developer_sponsored" = "player_metered",
    private readonly onChatDelivered?: (audience: string, text: string) => void,
    public readonly originToken: string = "",
    public readonly roomId: string = "",
    public readonly factionId: string = ""
  ) {}

  static async connectQuick(config: QuickOriginConfig): Promise<OriginSDK> {
    let resolvedUrl = config.hubOriginUrl ?? "auto";
    if (resolvedUrl === "auto") {
      try {
        resolvedUrl = await discoverLocalHubOriginUrl();
      } catch {
        resolvedUrl = config.fallbackUrl ?? "http://127.0.0.1:19210";
      }
    }
    const sdk = new OriginSDK(
      config.slotId,
      resolvedUrl,
      config.roleKind ?? "companion",
      config.billingMode ?? "player_metered",
      config.onChatDelivered,
      config.originToken ?? "",
      config.roomId ?? "",
      config.factionId ?? ""
    );
    const defaultActors = config.actors ?? (sdk.roleKind === "independent_npc" ? [] : ["companion", "player"]);
    for (const act of defaultActors) sdk.bindActorSubject(act, {});
    if (config.setup) await config.setup(sdk);
    await sdk.publishCapabilitiesAndStartOutboundStream(config.publishOptions);
    return sdk;
  }

  async close(notifyOffline = true): Promise<void> {
    for (const execId of [...this.inFlight.keys()]) {
      await this.cancelInFlightStep(execId);
    }
    if (notifyOffline) {
      try {
        await this.reportPresenceChanged(false, 0);
      } catch {}
    }
    this.stopOutboundExecuteStream();
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.close(true);
  }

  registerCapability(def: CapabilityEntry, handler: DomainEffectHandler): void {
    const actors =
      this.roleKind === "independent_npc"
        ? def.permitted_actors.filter((a) => a !== "player")
        : [...def.permitted_actors];
    this.capabilities.set(def.method_id, {
      def: {
        ...def,
        permitted_actors: actors,
        sustained: def.sustained ?? false,
        risk_flags: def.risk_flags ?? [],
        default_policy: def.default_policy ?? "allow",
      },
      handler,
    });
  }

  bindActorSubject(actor: string, instance: unknown): void {
    this.actors.set(actor, instance);
  }

  bindStateModule(
    name: string,
    snapshotFn: () => Record<string, unknown>,
    grantsFn: () => ActiveMethodGrant[]
  ): void {
    this.modules.set(name, { snapshotFn, grantsFn });
  }

  attachExtensionData(extName: string, data: Record<string, unknown>): void {
    this.extensionsData[extName] = { ...data };
  }

  async publishCapabilitiesAndStartOutboundStream(options?: SlotPublishOptions): Promise<void> {
    await this.startOutboundExecuteStream();
    const sortedDefs = [...this.capabilities.values()]
      .sort((a, b) => (a.def.method_id < b.def.method_id ? -1 : 1))
      .map((c) => c.def);
    const capDigest = await jcsSha256("g2a.cap.v1", sortedDefs);
    const capPayload: Record<string, unknown> = {
      slot_id: this.slotId,
      role_kind: this.roleKind,
      billing_mode: this.billingMode,
      origin_execute_url: "pull://origin.execute",
      capability_table_digest: capDigest,
      capabilities: sortedDefs,
    };
    if (options?.disabled) capPayload.disabled = true;
    if (options?.max_sessions && options.max_sessions > 0) capPayload.max_sessions = options.max_sessions;
    if (options?.shared_cooldown_ms && options.shared_cooldown_ms > 0) capPayload.shared_cooldown_ms = options.shared_cooldown_ms;
    if (options?.allowlisted_cores?.length) capPayload.allowlisted_cores = [...options.allowlisted_cores];
    if (options?.allowlist_criteria_url) capPayload.allowlist_criteria_url = options.allowlist_criteria_url;
    if (this.roomId) capPayload.room_id = this.roomId;
    if (this.factionId) capPayload.faction_id = this.factionId;
    await this.postToHub("origin.capabilities", capPayload);
    await this.flushFullSync();
  }

  async startOutboundExecuteStream(): Promise<void> {
    if (this.eventSource || this.streamAbort) return;
    const base = this.hubOriginBaseUrl.replace(/\/$/, "");
    const dispatchStreamedEnv = async (rawData: string) => {
      const env = parseStrictJson<Record<string, any>>(rawData);
      const reply = await this.handleOriginExecute(env.payload as OriginExecuteRequest);
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (this.originToken) headers.Authorization = `Bearer ${this.originToken}`;
      await fetch(`${base}/g2a/v1/origin/execute-reply`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          g2a_version: G2A_WIRE_VERSION,
          request_id: env.request_id,
          timestamp_ms: Date.now(),
          acceptance: "accepted",
          result: reply,
          error: null,
        }),
      });
    };

    if (typeof EventSource !== "undefined") {
      const tokenParam = this.originToken ? `&access_token=${encodeURIComponent(this.originToken)}` : "";
      const url = `${base}/g2a/v1/origin/execute-stream?slot_id=${encodeURIComponent(this.slotId)}${tokenParam}`;
      this.eventSource = new EventSource(url);
      this.eventSource.addEventListener("origin.execute", (ev: MessageEvent) => {
        void dispatchStreamedEnv(String(ev.data));
      });
      return;
    }

    // Universal fetch ReadableStream SSE consumer for Node 20+, Deno, Bun, & Workers
    const abort = new AbortController();
    this.streamAbort = abort;
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    if (this.originToken) headers.Authorization = `Bearer ${this.originToken}`;
    const resp = await fetch(`${base}/g2a/v1/origin/execute-stream?slot_id=${encodeURIComponent(this.slotId)}`, {
      headers,
      signal: abort.signal,
    });
    if (!resp.ok || !resp.body) return;
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    void (async () => {
      let leftover = "";
      let dataLines: string[] = [];
      try {
        while (!abort.signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          leftover += dec.decode(value, { stream: true });
          let nl: number;
          while ((nl = leftover.indexOf("\n")) >= 0) {
            const ln = leftover.slice(0, nl).replace(/\r$/, "");
            leftover = leftover.slice(nl + 1);
            if (ln === "") {
              if (dataLines.length > 0) {
                const joined = dataLines.join("\n");
                dataLines = [];
                await dispatchStreamedEnv(joined);
              }
            } else if (ln.startsWith("data:")) {
              dataLines.push(ln.slice(5).replace(/^ /, ""));
            }
          }
        }
      } catch {}
    })();
  }

  stopOutboundExecuteStream(): void {
    this.eventSource?.close();
    this.eventSource = null;
    this.streamAbort?.abort();
    this.streamAbort = null;
  }

  async flushDirtySync(significantEvent = false): Promise<boolean> {
    if (!this.syncEnabled && this.pendingSettled.length === 0) return false;
    const { brief, grants } = this.collectView();
    const hasExt = Object.keys(this.extensionsData).length > 0;
    const briefDigest = await jcsSha256("g2a.brief.v1", hasExt ? { brief, ext: this.extensionsData } : brief);

    const upserted: ActiveMethodGrant[] = [];
    const removed: Array<{ method_id: string; actor: string }> = [];
    const curCanon = new Map<string, string>();

    for (const [key, g] of grants.entries()) {
      const canon = canonicalizeJcs(g);
      curCanon.set(key, canon);
      if (this.lastSentMethods.get(key) !== canon) upserted.push(g);
    }
    for (const oldKey of this.lastSentMethods.keys()) {
      if (!curCanon.has(oldKey)) {
        const [method_id, actor] = oldKey.split("|");
        removed.push({ method_id, actor });
      }
    }

    const settledBatch = this.pendingSettled.splice(0, this.pendingSettled.length);
    if (briefDigest === this.lastBriefDigest && upserted.length === 0 && removed.length === 0 && settledBatch.length === 0) {
      return false;
    }

    const prevRev = this.currentRev++;
    this.lastBriefDigest = briefDigest;
    this.lastSentMethods = curCanon;
    await this.postToHub("origin.sync", {
      slot_id: this.slotId,
      epoch: this.currentEpoch,
      rev: this.currentRev,
      base_rev: prevRev,
      mode: "delta",
      significant_event: significantEvent,
      state_brief: brief,
      upserted_methods: upserted,
      removed_methods: removed,
      settled_executions: settledBatch,
      ...(hasExt ? { extensions_data: { ...this.extensionsData } } : {}),
    });
    return true;
  }

  async flushFullSync(): Promise<void> {
    const { brief, grants } = this.collectView();
    const hasExt = Object.keys(this.extensionsData).length > 0;
    this.currentRev++;
    this.lastBriefDigest = await jcsSha256("g2a.brief.v1", hasExt ? { brief, ext: this.extensionsData } : brief);
    this.lastSentMethods.clear();
    for (const [k, g] of grants.entries()) this.lastSentMethods.set(k, canonicalizeJcs(g));
    const settledBatch = this.pendingSettled.splice(0, this.pendingSettled.length);
    await this.postToHub("origin.sync", {
      slot_id: this.slotId,
      epoch: this.currentEpoch,
      rev: this.currentRev,
      mode: "full",
      significant_event: true,
      state_brief: brief,
      upserted_methods: [...grants.values()],
      removed_methods: [],
      settled_executions: settledBatch,
      ...(hasExt ? { extensions_data: { ...this.extensionsData } } : {}),
    });
  }

  async handleOriginExecute(req: OriginExecuteRequest): Promise<OriginExecuteReply> {
    if (req.kind === "cancel") return this.cancelInFlightStep(req.execution_id);
    if (req.kind === "deliver_chat") {
      this.onChatDelivered?.(String(req.args.audience ?? "player_only"), String(req.args.text ?? ""));
      return { execution_id: req.execution_id, state: "succeeded" };
    }
    const cached = this.ledger.get(req.execution_id);
    if (cached) return cached;

    const reg = req.method_id ? this.capabilities.get(req.method_id) : undefined;
    if (req.epoch !== this.currentEpoch || !reg || !req.actor || !this.actors.has(req.actor)) {
      return { execution_id: req.execution_id, state: "failed", failure_reason: "invalid_context_or_actor" };
    }
    if (!reg.def.permitted_actors.includes(req.actor)) {
      return { execution_id: req.execution_id, state: "failed", failure_reason: "permission_denied" };
    }
    const schemaErr = validateArgsAgainstSchema(req.args ?? {}, reg.def.params_schema);
    if (schemaErr) {
      return { execution_id: req.execution_id, state: "failed", failure_reason: schemaErr };
    }
    const out = reg.handler(req.actor, this.actors.get(req.actor), req.args, req.execution_id);
    if (out.kind === "executing") {
      this.inFlight.set(req.execution_id, out.cancel_fn);
      const rep: OriginExecuteReply = { execution_id: req.execution_id, state: "executing" };
      this.storeLedgerReply(rep);
      return rep;
    }
    const rep: OriginExecuteReply =
      out.kind === "succeeded"
        ? { execution_id: req.execution_id, state: "succeeded", effect_summary: out.effect_summary }
        : { execution_id: req.execution_id, state: "failed", failure_reason: out.reason };
    this.storeLedgerReply(rep);
    await this.flushDirtySync(false);
    return rep;
  }

  private storeLedgerReply(rep: OriginExecuteReply): void {
    this.ledger.delete(rep.execution_id);
    this.ledger.set(rep.execution_id, rep);
    if (this.ledger.size > MAX_EXECUTION_LEDGER_ENTRIES) {
      for (const oldId of this.ledger.keys()) {
        if (this.ledger.size <= MAX_EXECUTION_LEDGER_ENTRIES) break;
        if (!this.inFlight.has(oldId)) this.ledger.delete(oldId);
      }
    }
  }

  async completeStep(executionId: string, succeeded: boolean, summary?: Record<string, unknown>, reason?: string): Promise<void> {
    await this.completeStepsBatch([{ executionId, succeeded, summary, reason }]);
  }

  async completeStepsBatch(
    settlements: Array<{
      executionId: string;
      succeeded: boolean;
      summary?: Record<string, unknown>;
      reason?: string;
    }>
  ): Promise<number> {
    let count = 0;
    for (const s of settlements) {
      if (!this.inFlight.delete(s.executionId)) continue;
      const rep: OriginExecuteReply = {
        execution_id: s.executionId,
        state: s.succeeded ? "succeeded" : "failed",
        effect_summary: s.summary,
        failure_reason: s.reason,
      };
      this.storeLedgerReply(rep);
      this.pendingSettled.push(rep);
      count++;
    }
    if (count > 0) await this.flushDirtySync(false);
    return count;
  }

  async cancelInFlightStep(executionId: string): Promise<OriginExecuteReply> {
    const cancelFn = this.inFlight.get(executionId);
    if (cancelFn) {
      this.inFlight.delete(executionId);
      cancelFn();
      const rep: OriginExecuteReply = { execution_id: executionId, state: "cancelled", failure_reason: "cancelled_by_hub" };
      this.storeLedgerReply(rep);
      this.pendingSettled.push(rep);
      await this.flushDirtySync(false);
      return rep;
    }
    return this.ledger.get(executionId) ?? { execution_id: executionId, state: "cancelled" };
  }

  async reportPlayerCommand(text: string): Promise<void> {
    await this.flushDirtySync(true);
    await this.postToHub("origin.player_event", {
      slot_id: this.slotId,
      kind: "player_command",
      text,
    });
  }

  async reportPresenceChanged(online: boolean, remainingOnlineHumans = 0): Promise<void> {
    this.syncEnabled = online || remainingOnlineHumans > 0;
    await this.postToHub("origin.player_event", {
      slot_id: this.slotId,
      kind: "presence_changed",
      online,
      remaining_online_humans: remainingOnlineHumans,
    });
  }

  async reportEmergencyStop(): Promise<void> {
    for (const execId of [...this.inFlight.keys()]) {
      await this.cancelInFlightStep(execId);
    }
    await this.postToHub("origin.player_event", {
      slot_id: this.slotId,
      kind: "emergency_stop",
    });
  }

  async advanceEpoch(): Promise<void> {
    this.currentEpoch += 1;
    this.currentRev = 0;
    this.lastSentMethods.clear();
    for (const execId of [...this.inFlight.keys()]) {
      await this.cancelInFlightStep(execId);
    }
    await this.flushFullSync();
  }

  async reportQueueApproval(
    sessionId: string,
    queueId: string,
    queueDigest: string,
    fingerprint: string,
    approved: boolean
  ): Promise<void> {
    await this.postToHub("origin.player_event", {
      slot_id: this.slotId,
      kind: "queue_approval",
      session_id: sessionId,
      queue_id: queueId,
      queue_digest: queueDigest,
      fingerprint,
      approved,
    });
  }

  async reportNpcHostHandoff(newPlayerId: string): Promise<void> {
    if (this.roleKind !== "independent_npc" || this.billingMode !== "player_metered") {
      throw new Error("host_handoff requires player_metered independent_npc slot");
    }
    const cleaned = newPlayerId.trim();
    if (!cleaned) throw new Error("new_player_id is required");
    for (const execId of [...this.inFlight.keys()]) {
      await this.cancelInFlightStep(execId);
    }
    this.syncEnabled = true;
    await this.postToHub("origin.player_event", {
      slot_id: this.slotId,
      kind: "host_handoff",
      new_player_id: cleaned,
    });
  }

  private static parseAnchorMeters(anchor?: string): number {
    if (!anchor) return 1e9;
    const s = anchor.trim();
    if (!s.endsWith("m")) return 1e9;
    const token = s.slice(0, -1).split(" ").pop() ?? "";
    const d = Number(token);
    return Number.isFinite(d) ? d : 1e9;
  }

  private static prioritizeTargets(targets: TargetCandidate[]): TargetCandidate[] {
    const dedup = new Map<string, TargetCandidate>();
    for (const t of targets) {
      if (!dedup.has(t.target_id) || t.readiness === "ready") {
        dedup.set(t.target_id, t);
      }
    }
    return [...dedup.values()]
      .sort((a, b) => {
        const aAlert = a.alert_tag ? 0 : 1;
        const bAlert = b.alert_tag ? 0 : 1;
        if (aAlert !== bAlert) return aAlert - bAlert;
        const aPoint = a.player_anchor?.toLowerCase().includes("pointed") ? 0 : 1;
        const bPoint = b.player_anchor?.toLowerCase().includes("pointed") ? 0 : 1;
        if (aPoint !== bPoint) return aPoint - bPoint;
        const aReady = a.readiness === "ready" ? 0 : 1;
        const bReady = b.readiness === "ready" ? 0 : 1;
        if (aReady !== bReady) return aReady - bReady;
        const distDiff = OriginSDK.parseAnchorMeters(a.actor_anchor) - OriginSDK.parseAnchorMeters(b.actor_anchor);
        if (Math.abs(distDiff) > 1e-6) return distDiff;
        return a.target_id < b.target_id ? -1 : a.target_id > b.target_id ? 1 : 0;
      })
      .slice(0, MAX_CANDIDATE_TARGETS_PER_METHOD);
  }

  private collectView(): { brief: Record<string, unknown>; grants: Map<string, ActiveMethodGrant> } {
    const brief: Record<string, unknown> = {};
    const grants = new Map<string, ActiveMethodGrant>();
    for (const [name, mod] of this.modules.entries()) {
      brief[name] = mod.snapshotFn();
      for (const g of mod.grantsFn()) {
        const key = `${g.method_id}|${g.actor}`;
        const prev = grants.get(key);
        const mergedTargets = OriginSDK.prioritizeTargets(prev ? [...prev.targets, ...g.targets] : g.targets);
        const overall: Readiness =
          prev?.readiness === "ready" || g.readiness === "ready" || mergedTargets.some((t) => t.readiness === "ready")
            ? "ready"
            : "staged";
        grants.set(key, {
          method_id: g.method_id,
          actor: g.actor,
          readiness: overall,
          targets: mergedTargets,
        });
      }
    }
    return { brief, grants };
  }

  private async postToHub(operation: string, payload: unknown): Promise<void> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (this.originToken) headers.Authorization = `Bearer ${this.originToken}`;
    const resp = await fetch(`${this.hubOriginBaseUrl.replace(/\/$/, "")}/g2a/v1/origin`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        g2a_version: G2A_WIRE_VERSION,
        request_id: `ts-${crypto.randomUUID()}`,
        timestamp_ms: Date.now(),
        operation,
        payload,
        extensions: {},
      }),
    });
    if (!resp.ok) {
      throw new Error(`Hub rejected ${operation} (${resp.status}): ${await resp.text()}`);
    }
  }
}

export { canonicalizeJcs, jcsSha256, parseStrictJson };
