// Zero-build ES2022 runtime module for @g2a/interaction-origin (G2A V0.0.1).
export const G2A_WIRE_VERSION = "0.0.1";
export const MAX_CANDIDATE_TARGETS_PER_METHOD = 24;
export const MAX_EXECUTION_LEDGER_ENTRIES = 512;

function assertNoLoneSurrogates(s) {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      if (i + 1 >= s.length) throw new Error("Lone high UTF-16 surrogate in strict JSON");
      const low = s.charCodeAt(i + 1);
      if (low < 0xdc00 || low > 0xdfff) throw new Error("Unpaired high UTF-16 surrogate in strict JSON");
      i++;
    } else if (c >= 0xdc00 && c <= 0xdfff) {
      throw new Error("Lone low UTF-16 surrogate in strict JSON");
    }
  }
}

export function parseStrictJson(raw) {
  let pos = 0;
  const n = raw.length;
  const skipWs = () => {
    while (pos < n && (raw[pos] === " " || raw[pos] === "\t" || raw[pos] === "\r" || raw[pos] === "\n")) pos++;
  };
  const scanString = () => {
    const start = pos;
    pos++;
    while (pos < n) {
      const ch = raw[pos++];
      if (ch === "\\") pos += raw[pos] === "u" ? 5 : 1;
      else if (ch === '"') {
        const decoded = JSON.parse(raw.slice(start, pos));
        assertNoLoneSurrogates(decoded);
        return decoded;
      }
    }
    throw new Error("Unterminated JSON string");
  };
  const scanValue = (depth) => {
    if (depth > 64) throw new Error("JSON nesting depth exceeds 64");
    skipWs();
    if (pos >= n) throw new Error("Unexpected end of JSON");
    const c = raw[pos];
    if (c === '"') scanString();
    else if (c === "{") {
      pos++;
      skipWs();
      if (raw[pos] === "}") { pos++; return; }
      const seen = new Set();
      while (pos < n) {
        skipWs();
        if (raw[pos] !== '"') throw new Error("Expected string key in JSON object");
        const key = scanString();
        if (seen.has(key)) throw new Error(`Duplicate JSON object key: ${key}`);
        seen.add(key);
        skipWs();
        if (raw[pos++] !== ":") throw new Error("Expected ':' after JSON key");
        scanValue(depth + 1);
        skipWs();
        const sep = raw[pos++];
        if (sep === "}") break;
        if (sep !== ",") throw new Error("Expected ',' or '}' in JSON object");
      }
    } else if (c === "[") {
      pos++;
      skipWs();
      if (raw[pos] === "]") { pos++; return; }
      while (pos < n) {
        scanValue(depth + 1);
        skipWs();
        const sep = raw[pos++];
        if (sep === "]") break;
        if (sep !== ",") throw new Error("Expected ',' or ']' in JSON array");
      }
    } else {
      while (pos < n && !",]} \t\r\n".includes(raw[pos])) pos++;
    }
  };
  scanValue(0);
  skipWs();
  if (pos !== n) throw new Error("Trailing characters in strict JSON");
  return JSON.parse(raw);
}

export function canonicalizeJcs(val) {
  if (val === null || val === undefined) return "null";
  if (typeof val === "boolean") return val ? "true" : "false";
  if (typeof val === "number") {
    if (!Number.isFinite(val)) throw new Error("G2A strict JSON forbids NaN or Infinity");
    return Object.is(val, -0) ? "0" : JSON.stringify(val);
  }
  if (typeof val === "string") {
    assertNoLoneSurrogates(val);
    return JSON.stringify(val);
  }
  if (Array.isArray(val)) return "[" + val.map((x) => canonicalizeJcs(x)).join(",") + "]";
  if (typeof val === "object") {
    const keys = Object.keys(val)
      .filter((k) => val[k] !== undefined)
      .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
    return "{" + keys.map((k) => {
      assertNoLoneSurrogates(k);
      return `${JSON.stringify(k)}:${canonicalizeJcs(val[k])}`;
    }).join(",") + "}";
  }
  throw new Error(`Unsupported JCS value type: ${typeof val}`);
}

export async function jcsSha256(domainTag, val) {
  const enc = new TextEncoder();
  const prefix = enc.encode(domainTag + "\0");
  const body = enc.encode(canonicalizeJcs(val));
  const merged = new Uint8Array(prefix.length + body.length);
  merged.set(prefix, 0);
  merged.set(body, prefix.length);
  const hashBuf = await globalThis.crypto.subtle.digest("SHA-256", merged);
  const hex = Array.from(new Uint8Array(hashBuf)).map((b) => b.toString(16).padStart(2, "0")).join("");
  return `sha256:${hex}`;
}

export function formatPlayerDeicticAnchor(player, target, isPointed = false) {
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

export function validateArgsAgainstSchema(args, schema, prefix = "", depth = 0) {
  if (depth > 16) return "schema_depth_exceeded";
  if (!schema || Object.keys(schema).length === 0) return null;
  if (Array.isArray(schema.required)) {
    for (const f of schema.required) {
      if (typeof f === "string" && !(f in args)) return `missing_required_arg:${prefix ? `${prefix}.${f}` : f}`;
    }
  }
  const props = schema.properties;
  if (props && typeof props === "object") {
    if (schema.additionalProperties === false) {
      for (const k of Object.keys(args)) if (!(k in props)) return `unexpected_arg:${prefix ? `${prefix}.${k}` : k}`;
    }
    for (const [k, v] of Object.entries(args)) {
      if (props[k]) {
        const err = validateFieldSchema(prefix ? `${prefix}.${k}` : k, v, props[k], depth + 1);
        if (err) return err;
      }
    }
  }
  return null;
}

function validateFieldSchema(name, val, spec, depth) {
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
  if (ptype === "integer") return typeof val === "number" && Number.isInteger(val) ? checkNumBounds(name, val, spec) : `invalid_type:${name}`;
  if (ptype === "number") return typeof val === "number" && Number.isFinite(val) ? checkNumBounds(name, val, spec) : `invalid_type:${name}`;
  if (ptype === "array") {
    if (!Array.isArray(val)) return `invalid_type:${name}`;
    if (typeof spec.minItems === "number" && val.length < spec.minItems) return `min_items:${name}`;
    if (typeof spec.maxItems === "number" && val.length > spec.maxItems) return `max_items:${name}`;
    if (spec.items && typeof spec.items === "object") {
      for (let i = 0; i < val.length; i++) {
        const err = validateFieldSchema(`${name}[${i}]`, val[i], spec.items, depth + 1);
        if (err) return err;
      }
    }
    return null;
  }
  if (ptype === "object") {
    if (!val || typeof val !== "object" || Array.isArray(val)) return `invalid_type:${name}`;
    return validateArgsAgainstSchema(val, spec, name, depth + 1);
  }
  return null;
}

function checkNumBounds(name, v, spec) {
  if (typeof spec.minimum === "number" && v < spec.minimum) return `below_minimum:${name}`;
  if (typeof spec.maximum === "number" && v > spec.maximum) return `above_maximum:${name}`;
  if (typeof spec.exclusiveMinimum === "number" && v <= spec.exclusiveMinimum) return `below_exclusive_minimum:${name}`;
  if (typeof spec.exclusiveMaximum === "number" && v >= spec.exclusiveMaximum) return `above_exclusive_maximum:${name}`;
  return null;
}

export async function discoverLocalHubOriginUrl(explicitPath) {
  const fsMod = await import("node:fs/promises");
  const pathMod = await import("node:path");
  const osMod = await import("node:os");
  let p = explicitPath || process.env.G2A_HUB_RUNTIME_FILE || "";
  if (!p) {
    if (process.platform === "win32" && process.env.LOCALAPPDATA) p = pathMod.join(process.env.LOCALAPPDATA, "G2A", "hub.runtime.json");
    else p = pathMod.join(osMod.homedir(), ".g2a", "hub.runtime.json");
  }
  const doc = parseStrictJson(await fsMod.readFile(p, "utf8"));
  if (typeof doc.origin_url !== "string" || !doc.origin_url) throw new Error("Missing origin_url in Hub runtime manifest");
  return doc.origin_url.replace(/\/$/, "");
}

export class OriginSDK {
  constructor(
    slotId,
    hubOriginBaseUrl = "http://127.0.0.1:19210",
    roleKind = "companion",
    billingMode = "player_metered",
    onChatDelivered = undefined,
    originToken = "",
    roomId = "",
    factionId = ""
  ) {
    this.slotId = slotId;
    this.hubOriginBaseUrl = hubOriginBaseUrl;
    this.roleKind = roleKind;
    this.billingMode = billingMode;
    this.onChatDelivered = onChatDelivered;
    this.originToken = originToken;
    this.roomId = roomId;
    this.factionId = factionId;

    this.capabilities = new Map();
    this.actors = new Map();
    this.modules = new Map();
    this.inFlight = new Map();
    this.ledger = new Map();
    this.pendingSettled = [];
    this.lastSentMethods = new Map();
    this.lastBriefDigest = "";
    this.extensionsData = {};
    this.visualRef = null;
    this.audioRef = null;
    this.allowedTimelineTags = null;
    this.framesSynced = 0;
    this.zeroChangeSkips = 0;
    this.eventSource = null;
    this.streamAbort = null;
    this.currentEpoch = 1;
    this.currentRev = 0;
    this.syncEnabled = true;
  }

  static async connectQuick(config) {
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
    await sdk.publishCapabilitiesAndStartOutboundStream(config.publishOptions ?? {});
    return sdk;
  }

  async close(notifyOffline = true) {
    for (const execId of [...this.inFlight.keys()]) await this.cancelInFlightStep(execId);
    if (notifyOffline) {
      try { await this.reportPresenceChanged(false, 0); } catch {}
    }
    this.stopOutboundExecuteStream();
  }

  async [Symbol.asyncDispose]() {
    await this.close(true);
  }

  registerCapability(def, handler) {
    const actors = this.roleKind === "independent_npc" ? def.permitted_actors.filter((a) => a !== "player") : [...def.permitted_actors];
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

  bindActorSubject(actor, instance) {
    this.actors.set(actor, instance);
  }

  bindStateModule(name, snapshotFn, grantsFn) {
    this.modules.set(name, { snapshotFn, grantsFn });
  }

  attachExtensionData(extName, data) {
    this.extensionsData[extName] = { ...data };
  }

  attachVisualFrame(visualRef) {
    this.visualRef = { ...visualRef };
  }

  attachAudioSegment(audioRef) {
    this.audioRef = { ...audioRef };
  }

  setAllowedTimelineTags(tags) {
    this.allowedTimelineTags = [...tags];
  }

  async publishCapabilitiesAndStartOutboundStream(options = {}) {
    await this.startOutboundExecuteStream();
    const sortedDefs = [...this.capabilities.values()]
      .sort((a, b) => (a.def.method_id < b.def.method_id ? -1 : 1))
      .map((c) => c.def);
    const capDigest = await jcsSha256("g2a.cap.v1", sortedDefs);
    const capPayload = {
      slot_id: this.slotId,
      role_kind: this.roleKind,
      billing_mode: this.billingMode,
      origin_execute_url: "pull://origin.execute",
      capability_table_digest: capDigest,
      capabilities: sortedDefs,
    };
    if (options.disabled) capPayload.disabled = true;
    if (options.max_sessions > 0) capPayload.max_sessions = options.max_sessions;
    if (options.shared_cooldown_ms > 0) capPayload.shared_cooldown_ms = options.shared_cooldown_ms;
    if (options.allowlisted_cores?.length) capPayload.allowlisted_cores = [...options.allowlisted_cores];
    if (options.allowlist_criteria_url) capPayload.allowlist_criteria_url = options.allowlist_criteria_url;
    if (this.roomId) capPayload.room_id = this.roomId;
    if (this.factionId) capPayload.faction_id = this.factionId;
    await this.postToHub("origin.capabilities", capPayload);
    await this.flushFullSync();
  }

  async startOutboundExecuteStream() {
    if (this.eventSource || this.streamAbort) return;
    const base = this.hubOriginBaseUrl.replace(/\/$/, "");
    const dispatchStreamedEnv = async (rawData) => {
      const env = parseStrictJson(rawData);
      const reply = await this.handleOriginExecute(env.payload);
      const headers = { "Content-Type": "application/json" };
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
      this.eventSource = new EventSource(`${base}/g2a/v1/origin/execute-stream?slot_id=${encodeURIComponent(this.slotId)}${tokenParam}`);
      this.eventSource.addEventListener("origin.execute", (ev) => void dispatchStreamedEnv(String(ev.data)));
      return;
    }

    const abort = new AbortController();
    this.streamAbort = abort;
    const headers = { Accept: "text/event-stream" };
    if (this.originToken) headers.Authorization = `Bearer ${this.originToken}`;
    const resp = await fetch(`${base}/g2a/v1/origin/execute-stream?slot_id=${encodeURIComponent(this.slotId)}`, { headers, signal: abort.signal });
    if (!resp.ok || !resp.body) return;
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    void (async () => {
      let leftover = "";
      let dataLines = [];
      try {
        while (!abort.signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          leftover += dec.decode(value, { stream: true });
          let nl;
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

  stopOutboundExecuteStream() {
    this.eventSource?.close();
    this.eventSource = null;
    this.streamAbort?.abort();
    this.streamAbort = null;
  }

  async flushDirtySync(significantEvent = false) {
    if (!this.syncEnabled && this.pendingSettled.length === 0) return false;
    const { brief, grants } = this.collectView();
    const hasExt = Object.keys(this.extensionsData).length > 0;
    const hasExtra = hasExt || this.visualRef !== null || this.audioRef !== null || this.allowedTimelineTags !== null;
    const briefDigest = await jcsSha256(
      "g2a.brief.v1",
      hasExtra
        ? { brief, ext: hasExt ? this.extensionsData : null, visual: this.visualRef, audio: this.audioRef, timelines: this.allowedTimelineTags }
        : brief
    );
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
    if (briefDigest === this.lastBriefDigest && upserted.length === 0 && removed.length === 0 && settledBatch.length === 0) {
      this.zeroChangeSkips++;
      return false;
    }
    const prevRev = this.currentRev++;
    this.framesSynced++;
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
      ...(this.visualRef ? { visual_ref: { ...this.visualRef } } : {}),
      ...(this.audioRef ? { audio_ref: { ...this.audioRef } } : {}),
      ...(this.allowedTimelineTags ? { allowed_timeline_tags: [...this.allowedTimelineTags] } : {}),
      ...(hasExt ? { extensions_data: { ...this.extensionsData } } : {}),
    });
    return true;
  }

  async flushFullSync() {
    const { brief, grants } = this.collectView();
    const hasExt = Object.keys(this.extensionsData).length > 0;
    const hasExtra = hasExt || this.visualRef !== null || this.audioRef !== null || this.allowedTimelineTags !== null;
    this.currentRev++;
    this.framesSynced++;
    this.lastBriefDigest = await jcsSha256(
      "g2a.brief.v1",
      hasExtra
        ? { brief, ext: hasExt ? this.extensionsData : null, visual: this.visualRef, audio: this.audioRef, timelines: this.allowedTimelineTags }
        : brief
    );
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
      ...(this.visualRef ? { visual_ref: { ...this.visualRef } } : {}),
      ...(this.audioRef ? { audio_ref: { ...this.audioRef } } : {}),
      ...(this.allowedTimelineTags ? { allowed_timeline_tags: [...this.allowedTimelineTags] } : {}),
      ...(hasExt ? { extensions_data: { ...this.extensionsData } } : {}),
    });
  }

  async handleOriginExecute(req) {
    if (req.kind === "cancel") return this.cancelInFlightStep(req.execution_id);
    if (req.kind === "deliver_chat") {
      this.onChatDelivered?.(String(req.args?.audience ?? "player_only"), String(req.args?.text ?? ""));
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
    if (schemaErr) return { execution_id: req.execution_id, state: "failed", failure_reason: schemaErr };
    const out = reg.handler(req.actor, this.actors.get(req.actor), req.args ?? {}, req.execution_id);
    if (out.kind === "executing") {
      this.inFlight.set(req.execution_id, out.cancel_fn);
      const rep = { execution_id: req.execution_id, state: "executing" };
      this.storeLedgerReply(rep);
      return rep;
    }
    const rep = out.kind === "succeeded"
      ? { execution_id: req.execution_id, state: "succeeded", effect_summary: out.effect_summary }
      : { execution_id: req.execution_id, state: "failed", failure_reason: out.reason };
    this.storeLedgerReply(rep);
    await this.flushDirtySync(false);
    return rep;
  }

  storeLedgerReply(rep) {
    this.ledger.delete(rep.execution_id);
    this.ledger.set(rep.execution_id, rep);
    if (this.ledger.size > MAX_EXECUTION_LEDGER_ENTRIES) {
      for (const oldId of this.ledger.keys()) {
        if (this.ledger.size <= MAX_EXECUTION_LEDGER_ENTRIES) break;
        if (!this.inFlight.has(oldId)) this.ledger.delete(oldId);
      }
    }
  }

  async completeStep(executionId, succeeded, summary, reason) {
    await this.completeStepsBatch([{ executionId, succeeded, summary, reason }]);
  }

  async completeStepsBatch(settlements) {
    let count = 0;
    for (const s of settlements) {
      if (!this.inFlight.delete(s.executionId)) continue;
      const rep = {
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

  async cancelInFlightStep(executionId) {
    const cancelFn = this.inFlight.get(executionId);
    if (cancelFn) {
      this.inFlight.delete(executionId);
      cancelFn();
      const rep = { execution_id: executionId, state: "cancelled", failure_reason: "cancelled_by_hub" };
      this.storeLedgerReply(rep);
      this.pendingSettled.push(rep);
      await this.flushDirtySync(false);
      return rep;
    }
    return this.ledger.get(executionId) ?? { execution_id: executionId, state: "cancelled" };
  }

  async reportPlayerCommand(text) {
    await this.flushDirtySync(true);
    await this.postToHub("origin.player_event", { slot_id: this.slotId, kind: "player_command", text });
  }

  async reportPresenceChanged(online, remainingOnlineHumans = 0) {
    this.syncEnabled = online || remainingOnlineHumans > 0;
    await this.postToHub("origin.player_event", { slot_id: this.slotId, kind: "presence_changed", online, remaining_online_humans: remainingOnlineHumans });
  }

  async reportEmergencyStop() {
    for (const execId of [...this.inFlight.keys()]) await this.cancelInFlightStep(execId);
    await this.postToHub("origin.player_event", { slot_id: this.slotId, kind: "emergency_stop" });
  }

  async advanceEpoch() {
    this.currentEpoch += 1;
    this.currentRev = 0;
    this.lastSentMethods.clear();
    for (const execId of [...this.inFlight.keys()]) await this.cancelInFlightStep(execId);
    await this.flushFullSync();
  }

  async reportQueueApproval(sessionId, queueId, queueDigest, fingerprint, approved) {
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

  async reportNpcHostHandoff(newPlayerId) {
    if (this.roleKind !== "independent_npc" || this.billingMode !== "player_metered") {
      throw new Error("host_handoff requires player_metered independent_npc slot");
    }
    const cleaned = String(newPlayerId).trim();
    if (!cleaned) throw new Error("new_player_id is required");
    for (const execId of [...this.inFlight.keys()]) await this.cancelInFlightStep(execId);
    this.syncEnabled = true;
    await this.postToHub("origin.player_event", { slot_id: this.slotId, kind: "host_handoff", new_player_id: cleaned });
  }

  static parseAnchorMeters(anchor) {
    if (!anchor) return 1e9;
    const s = String(anchor).trim();
    if (!s.endsWith("m")) return 1e9;
    const d = Number(s.slice(0, -1).split(" ").pop() ?? "");
    return Number.isFinite(d) ? d : 1e9;
  }

  static prioritizeTargets(targets) {
    const dedup = new Map();
    for (const t of targets) {
      if (!dedup.has(t.target_id) || t.readiness === "ready") dedup.set(t.target_id, t);
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

  collectView() {
    const brief = {};
    const grants = new Map();
    for (const [name, mod] of this.modules.entries()) {
      brief[name] = mod.snapshotFn();
      for (const g of mod.grantsFn()) {
        const key = `${g.method_id}|${g.actor}`;
        const prev = grants.get(key);
        const mergedTargets = OriginSDK.prioritizeTargets(prev ? [...prev.targets, ...g.targets] : g.targets);
        const overall = prev?.readiness === "ready" || g.readiness === "ready" || mergedTargets.some((t) => t.readiness === "ready") ? "ready" : "staged";
        grants.set(key, { method_id: g.method_id, actor: g.actor, readiness: overall, targets: mergedTargets });
      }
    }
    return { brief, grants };
  }

  async postToHub(operation, payload) {
    const headers = { "Content-Type": "application/json" };
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
    if (!resp.ok) throw new Error(`Hub rejected ${operation} (${resp.status}): ${await resp.text()}`);
  }
}
