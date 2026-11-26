import { canonicalizeJcs, jcsSha256, parseStrictJson } from "./jcs.js";

export const G2A_WIRE_VERSION = "0.0.1";
export const MAX_CANDIDATE_TARGETS_PER_METHOD = 24;
export const MAX_EXECUTION_LEDGER_ENTRIES = 512;

export type PolicyMode = "allow" | "confirm" | "prohibit";
export type Readiness = "ready" | "staged";

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

/**
 * Tier 1 Interaction Origin SDK for Browser / HTML5 / WebGL / Tauri games.
 * Uses zero-inbound-port `EventSource` ("pull://origin.execute") so sandboxed browser tabs never bind TCP sockets.
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

  registerCapability(def: CapabilityEntry, handler: DomainEffectHandler): void {
    const actors = this.roleKind === "independent_npc"
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

  async publishCapabilitiesAndStartOutboundStream(): Promise<void> {
    this.startOutboundExecuteStream();
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
    if (this.roomId) capPayload.room_id = this.roomId;
    if (this.factionId) capPayload.faction_id = this.factionId;
    await this.postToHub("origin.capabilities", capPayload);
    await this.flushFullSync();
  }

  startOutboundExecuteStream(): void {
    if (typeof EventSource === "undefined" || this.eventSource) return;
    const tokenParam = this.originToken ? `&access_token=${encodeURIComponent(this.originToken)}` : "";
    const url = `${this.hubOriginBaseUrl.replace(/\/$/, "")}/g2a/v1/origin/execute-stream?slot_id=${encodeURIComponent(this.slotId)}${tokenParam}`;
    this.eventSource = new EventSource(url);
    this.eventSource.addEventListener("origin.execute", async (ev: MessageEvent) => {
      const env = parseStrictJson<Record<string, any>>(String(ev.data));
      const reply = await this.handleOriginExecute(env.payload as OriginExecuteRequest);
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (this.originToken) headers.Authorization = `Bearer ${this.originToken}`;
      await fetch(`${this.hubOriginBaseUrl.replace(/\/$/, "")}/g2a/v1/origin/execute-reply`, {
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
    });
  }

  stopOutboundExecuteStream(): void {
    this.eventSource?.close();
    this.eventSource = null;
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
    const reqFields = Array.isArray(reg.def.params_schema?.required) ? (reg.def.params_schema.required as string[]) : [];
    for (const f of reqFields) {
      if (!(f in (req.args ?? {}))) {
        return { execution_id: req.execution_id, state: "failed", failure_reason: `missing_required_arg:${f}` };
      }
    }
    const out = reg.handler(req.actor, this.actors.get(req.actor), req.args, req.execution_id);
    if (out.kind === "executing") {
      this.inFlight.set(req.execution_id, out.cancel_fn);
      const rep: OriginExecuteReply = { execution_id: req.execution_id, state: "executing" };
      this.storeLedgerReply(rep);
      return rep;
    }
    const rep: OriginExecuteReply = out.kind === "succeeded"
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
    if (!this.inFlight.delete(executionId)) return;
    const rep: OriginExecuteReply = {
      execution_id: executionId,
      state: succeeded ? "succeeded" : "failed",
      effect_summary: summary,
      failure_reason: reason,
    };
    this.storeLedgerReply(rep);
    this.pendingSettled.push(rep);
    await this.flushDirtySync(false);
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
    for (const execId of [...this.inFlight.keys()]) {
      await this.cancelInFlightStep(execId);
    }
    this.syncEnabled = true;
    await this.postToHub("origin.player_event", {
      slot_id: this.slotId,
      kind: "host_handoff",
      new_player_id: newPlayerId.trim(),
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
    await fetch(`${this.hubOriginBaseUrl.replace(/\/$/, "")}/g2a/v1/origin`, {
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
  }
}

export { canonicalizeJcs, jcsSha256, parseStrictJson };
