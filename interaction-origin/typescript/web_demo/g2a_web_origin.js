// Zero-dependency ES2022 G2A V0.0.1 Interaction Origin Browser Runtime (RFC 8785 JCS + Zero-Inbound-Port EventSource)

export const G2A_WIRE_VERSION = "0.0.1";
export const MAX_CANDIDATE_TARGETS_PER_METHOD = 24;

export function canonicalizeJcs(value) {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("Non-finite numbers forbidden in JCS");
    if (Object.is(value, -0)) return "0";
    return JSON.stringify(value);
  }
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map((v) => canonicalizeJcs(v)).join(",") + "]";
  if (typeof value === "object") {
    const keys = Object.keys(value)
      .filter((k) => value[k] !== undefined)
      .sort();
    return "{" + keys.map((k) => `${JSON.stringify(k)}:${canonicalizeJcs(value[k])}`).join(",") + "}";
  }
  throw new Error(`Unsupported JCS type: ${typeof value}`);
}

export async function jcsSha256(domainTag, value) {
  const bytes = new TextEncoder().encode(`${domainTag}\x00${canonicalizeJcs(value)}`);
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  const hex = Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return `sha256:${hex}`;
}

export class WebOriginSDK {
  constructor({
    slotId,
    hubOriginBaseUrl = "http://127.0.0.1:19210",
    roleKind = "companion",
    billingMode = "player_metered",
    originToken = "",
    roomId = "",
    factionId = "",
    onChatDelivered = null,
    onWireEvent = null,
  }) {
    this.slotId = slotId;
    this.hubOriginBaseUrl = hubOriginBaseUrl.replace(/\/$/, "");
    this.roleKind = roleKind;
    this.billingMode = billingMode;
    this.originToken = originToken;
    this.roomId = roomId;
    this.factionId = factionId;
    this.onChatDelivered = onChatDelivered;
    this.onWireEvent = onWireEvent;

    this.capabilities = new Map();
    this.actors = new Map();
    this.modules = new Map();
    this.inFlight = new Map();
    this.ledger = new Map();
    this.pendingSettled = [];
    this.lastSentMethods = new Map();
    this.lastBriefDigest = "";
    this.eventSource = null;

    this.currentEpoch = 1;
    this.currentRev = 0;
    this.syncEnabled = true;
  }

  registerCapability(def, handler) {
    const actors =
      this.roleKind === "independent_npc"
        ? def.permitted_actors.filter((a) => a !== "player")
        : [...def.permitted_actors];
    this.capabilities.set(def.method_id, {
      def: {
        method_id: def.method_id,
        name: def.name,
        description: def.description,
        permitted_actors: actors,
        params_schema: def.params_schema ?? {},
        sustained: Boolean(def.sustained),
        risk_flags: def.risk_flags ?? [],
        default_policy: def.default_policy ?? "allow",
      },
      handler,
    });
  }

  bindActorSubject(actor, instance) {
    this.actors.set(actor, instance);
  }

  bindStateModule(name, snapshotFn, grantsFn) {
    this.modules.set(name, { snapshotFn, grantsFn });
  }

  async connect() {
    this.startOutboundExecuteStream();
    const sortedDefs = [...this.capabilities.values()]
      .sort((a, b) => (a.def.method_id < b.def.method_id ? -1 : 1))
      .map((c) => c.def);
    const capDigest = await jcsSha256("g2a.cap.v1", sortedDefs);
    const payload = {
      slot_id: this.slotId,
      role_kind: this.roleKind,
      billing_mode: this.billingMode,
      origin_execute_url: "pull://origin.execute",
      capability_table_digest: capDigest,
      capabilities: sortedDefs,
    };
    if (this.roomId) payload.room_id = this.roomId;
    if (this.factionId) payload.faction_id = this.factionId;
    await this.postToHub("origin.capabilities", payload);
    await this.flushFullSync();
  }

  startOutboundExecuteStream() {
    if (this.eventSource) return;
    const tokenParam = this.originToken ? `&access_token=${encodeURIComponent(this.originToken)}` : "";
    const url = `${this.hubOriginBaseUrl}/g2a/v1/origin/execute-stream?slot_id=${encodeURIComponent(this.slotId)}${tokenParam}`;
    this.eventSource = new EventSource(url);
    this.eventSource.addEventListener("origin.execute", async (ev) => {
      const env = JSON.parse(ev.data);
      this.onWireEvent?.("IN  origin.execute", env.payload);
      const reply = await this.handleOriginExecute(env.payload);
      const headers = { "Content-Type": "application/json" };
      if (this.originToken) headers.Authorization = `Bearer ${this.originToken}`;
      await fetch(`${this.hubOriginBaseUrl}/g2a/v1/origin/execute-reply`, {
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
      this.onWireEvent?.("OUT execute-reply", reply);
    });
  }

  async flushDirtySync(significantEvent = false) {
    if (!this.syncEnabled && this.pendingSettled.length === 0) return false;
    const { brief, grants } = this.collectView();
    const briefDigest = await jcsSha256("g2a.brief.v1", brief);

    const upserted = [];
    const removed = [];
    const curCanon = new Map();

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
    if (
      briefDigest === this.lastBriefDigest &&
      upserted.length === 0 &&
      removed.length === 0 &&
      settledBatch.length === 0
    ) {
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
    });
    return true;
  }

  async flushFullSync() {
    const { brief, grants } = this.collectView();
    this.currentRev++;
    this.lastBriefDigest = await jcsSha256("g2a.brief.v1", brief);
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
    });
  }

  async handleOriginExecute(req) {
    if (req.kind === "cancel") return this.cancelInFlightStep(req.execution_id);
    if (req.kind === "deliver_chat") {
      this.onChatDelivered?.(String(req.args?.audience ?? "player_only"), String(req.args?.text ?? ""));
      return { execution_id: req.execution_id, state: "succeeded" };
    }
    if (this.ledger.has(req.execution_id)) return this.ledger.get(req.execution_id);

    const reg = req.method_id ? this.capabilities.get(req.method_id) : null;
    if (req.epoch !== this.currentEpoch || !reg || !req.actor || !this.actors.has(req.actor)) {
      return { execution_id: req.execution_id, state: "failed", failure_reason: "invalid_context_or_actor" };
    }
    const out = reg.handler(req.actor, this.actors.get(req.actor), req.args ?? {}, req.execution_id);
    if (out.kind === "executing") {
      this.inFlight.set(req.execution_id, out.cancel_fn);
      const rep = { execution_id: req.execution_id, state: "executing" };
      this.ledger.set(req.execution_id, rep);
      return rep;
    }
    const rep =
      out.kind === "succeeded"
        ? { execution_id: req.execution_id, state: "succeeded", effect_summary: out.effect_summary }
        : { execution_id: req.execution_id, state: "failed", failure_reason: out.reason };
    this.ledger.set(req.execution_id, rep);
    await this.flushDirtySync(false);
    return rep;
  }

  async completeStep(executionId, succeeded, summary = {}, reason = undefined) {
    if (!this.inFlight.delete(executionId)) return;
    const rep = {
      execution_id: executionId,
      state: succeeded ? "succeeded" : "failed",
      effect_summary: succeeded ? summary : undefined,
      failure_reason: succeeded ? undefined : reason,
    };
    this.ledger.set(executionId, rep);
    this.pendingSettled.push(rep);
    await this.flushDirtySync(false);
  }

  async cancelInFlightStep(executionId) {
    const cancelFn = this.inFlight.get(executionId);
    if (cancelFn) {
      this.inFlight.delete(executionId);
      cancelFn();
      const rep = { execution_id: executionId, state: "cancelled", failure_reason: "cancelled_by_hub" };
      this.ledger.set(executionId, rep);
      this.pendingSettled.push(rep);
      await this.flushDirtySync(false);
      return rep;
    }
    return this.ledger.get(executionId) ?? { execution_id: executionId, state: "cancelled" };
  }

  async reportPlayerCommand(text) {
    await this.flushDirtySync(true);
    await this.postToHub("origin.player_event", {
      slot_id: this.slotId,
      kind: "player_command",
      text,
    });
  }

  async reportEmergencyStop() {
    for (const execId of [...this.inFlight.keys()]) {
      await this.cancelInFlightStep(execId);
    }
    await this.postToHub("origin.player_event", {
      slot_id: this.slotId,
      kind: "emergency_stop",
    });
  }

  collectView() {
    const brief = {};
    const grants = new Map();
    for (const [name, mod] of this.modules.entries()) {
      brief[name] = mod.snapshotFn();
      for (const g of mod.grantsFn()) {
        grants.set(`${g.method_id}|${g.actor}`, {
          ...g,
          targets: g.targets.slice(0, MAX_CANDIDATE_TARGETS_PER_METHOD),
        });
      }
    }
    return { brief, grants };
  }

  async postToHub(operation, payload) {
    const headers = { "Content-Type": "application/json" };
    if (this.originToken) headers.Authorization = `Bearer ${this.originToken}`;
    this.onWireEvent?.(`OUT ${operation}`, payload);
    const resp = await fetch(`${this.hubOriginBaseUrl}/g2a/v1/origin`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        g2a_version: G2A_WIRE_VERSION,
        request_id: `web-${crypto.randomUUID().slice(0, 12)}`,
        timestamp_ms: Date.now(),
        operation,
        payload,
        extensions: {},
      }),
    });
    if (!resp.ok) {
      throw new Error(`Hub ${operation} failed: HTTP ${resp.status}`);
    }
  }
}
