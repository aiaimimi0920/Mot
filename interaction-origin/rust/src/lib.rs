//! Zero-dependency Rust Interaction Origin SDK for G2A V0.0.1.
//! Compatible with Bevy, Fyrox, Tauri, and custom Rust game hosts.

#![forbid(unsafe_code)]

pub mod jcs;
pub mod sha256;
pub mod transport;

use std::collections::{BTreeMap, VecDeque};
use std::sync::Mutex;

use jcs::{canonicalize_utf8, digest_sha256, JsonArray, JsonObject, JsonValue};

pub const G2A_WIRE_VERSION: &str = "0.0.1";
pub const MAX_CANDIDATE_TARGETS: usize = 24;
pub const MAX_EXECUTION_LEDGER: usize = 512;

#[derive(Clone, Debug)]
pub struct CapabilityEntry {
    pub method_id: String,
    pub name: String,
    pub description: String,
    pub permitted_actors: Vec<String>,
    pub params_schema: JsonObject,
    pub sustained: bool,
    pub risk_flags: Vec<String>,
    pub default_policy: String,
}

impl CapabilityEntry {
    pub fn to_json(&self) -> JsonValue {
        let mut o = JsonObject::new();
        o.insert("method_id".to_owned(), JsonValue::from(self.method_id.as_str()));
        o.insert("name".to_owned(), JsonValue::from(self.name.as_str()));
        o.insert("description".to_owned(), JsonValue::from(self.description.as_str()));
        o.insert(
            "permitted_actors".to_owned(),
            JsonValue::Array(self.permitted_actors.iter().map(|a| JsonValue::from(a.as_str())).collect()),
        );
        o.insert("params_schema".to_owned(), JsonValue::Object(self.params_schema.clone()));
        o.insert("sustained".to_owned(), JsonValue::from(self.sustained));
        o.insert(
            "risk_flags".to_owned(),
            JsonValue::Array(self.risk_flags.iter().map(|r| JsonValue::from(r.as_str())).collect()),
        );
        o.insert("default_policy".to_owned(), JsonValue::from(self.default_policy.as_str()));
        JsonValue::Object(o)
    }
}

#[derive(Clone, Debug)]
pub struct TargetCandidate {
    pub target_id: String,
    pub label: String,
    pub readiness: String,
    pub prerequisite_hint: Option<String>,
    pub actor_anchor: Option<String>,
    pub player_anchor: Option<String>,
    pub alert_tag: Option<String>,
}

impl TargetCandidate {
    pub fn to_json(&self) -> JsonValue {
        let mut o = JsonObject::new();
        o.insert("target_id".to_owned(), JsonValue::from(self.target_id.as_str()));
        o.insert("label".to_owned(), JsonValue::from(self.label.as_str()));
        o.insert("readiness".to_owned(), JsonValue::from(self.readiness.as_str()));
        if let Some(ref v) = self.prerequisite_hint {
            o.insert("prerequisite_hint".to_owned(), JsonValue::from(v.as_str()));
        }
        if let Some(ref v) = self.actor_anchor {
            o.insert("actor_anchor".to_owned(), JsonValue::from(v.as_str()));
        }
        if let Some(ref v) = self.player_anchor {
            o.insert("player_anchor".to_owned(), JsonValue::from(v.as_str()));
        }
        if let Some(ref v) = self.alert_tag {
            o.insert("alert_tag".to_owned(), JsonValue::from(v.as_str()));
        }
        JsonValue::Object(o)
    }
}

#[derive(Clone, Debug)]
pub struct ActiveMethodGrant {
    pub method_id: String,
    pub actor: String,
    pub readiness: String,
    pub targets: Vec<TargetCandidate>,
}

impl ActiveMethodGrant {
    pub fn to_json(&self) -> JsonValue {
        let mut o = JsonObject::new();
        o.insert("method_id".to_owned(), JsonValue::from(self.method_id.as_str()));
        o.insert("actor".to_owned(), JsonValue::from(self.actor.as_str()));
        o.insert("readiness".to_owned(), JsonValue::from(self.readiness.as_str()));
        o.insert(
            "targets".to_owned(),
            JsonValue::Array(self.targets.iter().map(TargetCandidate::to_json).collect()),
        );
        JsonValue::Object(o)
    }
}

#[derive(Clone, Debug)]
pub struct OriginExecuteRequest {
    pub slot_id: String,
    pub epoch: i64,
    pub kind: String,
    pub execution_id: String,
    pub method_id: Option<String>,
    pub actor: Option<String>,
    pub args: JsonObject,
}

impl OriginExecuteRequest {
    pub fn from_json(obj: &JsonObject) -> Result<Self, String> {
        let slot_id = obj
            .get("slot_id")
            .and_then(JsonValue::as_str)
            .ok_or_else(|| "Missing slot_id".to_owned())?
            .to_owned();
        let epoch = obj.get("epoch").and_then(JsonValue::as_i64).unwrap_or(1);
        let kind = obj
            .get("kind")
            .and_then(JsonValue::as_str)
            .unwrap_or("invoke")
            .to_owned();
        let execution_id = obj
            .get("execution_id")
            .and_then(JsonValue::as_str)
            .ok_or_else(|| "Missing execution_id".to_owned())?
            .to_owned();
        let method_id = obj.get("method_id").and_then(JsonValue::as_str).map(str::to_owned);
        let actor = obj.get("actor").and_then(JsonValue::as_str).map(str::to_owned);
        let args = obj
            .get("args")
            .and_then(JsonValue::as_object)
            .cloned()
            .unwrap_or_default();
        Ok(Self {
            slot_id,
            epoch,
            kind,
            execution_id,
            method_id,
            actor,
            args,
        })
    }
}

#[derive(Clone, Debug)]
pub struct OriginExecuteReply {
    pub execution_id: String,
    pub state: String,
    pub effect_summary: Option<JsonObject>,
    pub failure_reason: Option<String>,
}

impl OriginExecuteReply {
    pub fn to_json(&self) -> JsonValue {
        let mut o = JsonObject::new();
        o.insert("execution_id".to_owned(), JsonValue::from(self.execution_id.as_str()));
        o.insert("state".to_owned(), JsonValue::from(self.state.as_str()));
        if let Some(ref sum) = self.effect_summary {
            o.insert("effect_summary".to_owned(), JsonValue::Object(sum.clone()));
        }
        if let Some(ref reason) = self.failure_reason {
            o.insert("failure_reason".to_owned(), JsonValue::from(reason.as_str()));
        }
        JsonValue::Object(o)
    }
}

pub enum EffectOutcome {
    ImmediateSucceeded(JsonObject),
    ImmediateFailed(String),
    StartedExecuting(Box<dyn FnOnce() + Send + 'static>),
}

#[derive(Clone, Debug, Default)]
pub struct SlotPublishOptions {
    pub origin_execute_url: String,
    pub disabled: bool,
    pub max_sessions: i64,
    pub shared_cooldown_ms: i64,
    pub allowlisted_cores: Vec<String>,
    pub allowlist_criteria_url: String,
}

#[derive(Clone, Copy, Debug)]
pub struct Pose3D {
    pub x: f64,
    pub y: f64,
    pub z: f64,
    pub yaw_deg: f64,
}

impl Pose3D {
    pub fn distance_to(&self, other: &Pose3D) -> f64 {
        let dx = other.x - self.x;
        let dy = other.y - self.y;
        let dz = other.z - self.z;
        (dx * dx + dy * dy + dz * dz).sqrt()
    }

    /// Computes a 360-degree yaw-relative deictic anchor (e.g. "ahead 2.4m", "front-right 4.0m", "pointed_ray").
    pub fn format_player_deictic_anchor(&self, target: &Pose3D, is_pointed: bool) -> String {
        let dist = self.distance_to(target);
        if is_pointed {
            return format!("pointed_ray {dist:.1}m");
        }
        let dx = target.x - self.x;
        let dz = target.z - self.z;
        if (dx * dx + dz * dz) < 1e-6 {
            return format!("here {dist:.1}m");
        }
        let world_deg = dx.atan2(dz).to_degrees();
        let mut rel = (world_deg - self.yaw_deg) % 360.0;
        if rel > 180.0 {
            rel -= 360.0;
        } else if rel <= -180.0 {
            rel += 360.0;
        }
        let sector = if (-22.5..=22.5).contains(&rel) {
            "ahead"
        } else if (22.5..=67.5).contains(&rel) {
            "front-right"
        } else if (67.5..=112.5).contains(&rel) {
            "right"
        } else if (112.5..=157.5).contains(&rel) {
            "back-right"
        } else if (-67.5..-22.5).contains(&rel) {
            "front-left"
        } else if (-112.5..-67.5).contains(&rel) {
            "left"
        } else if (-157.5..-112.5).contains(&rel) {
            "back-left"
        } else {
            "behind"
        };
        format!("{sector} {dist:.1}m")
    }
}

pub fn validate_args_against_schema(args: &JsonObject, schema: &JsonObject) -> Result<(), String> {
    validate_object_schema(args, schema, "", 0)
}

fn validate_object_schema(
    args: &JsonObject,
    schema: &JsonObject,
    prefix: &str,
    depth: usize,
) -> Result<(), String> {
    if depth > 16 {
        return Err("schema_depth_exceeded".to_owned());
    }
    if schema.is_empty() {
        return Ok(());
    }
    if let Some(req_arr) = schema.get("required").and_then(JsonValue::as_array) {
        for item in req_arr {
            if let Some(req_key) = item.as_str() {
                if !args.contains_key(req_key) {
                    let qual = if prefix.is_empty() {
                        req_key.to_owned()
                    } else {
                        format!("{prefix}.{req_key}")
                    };
                    return Err(format!("missing_required_arg:{qual}"));
                }
            }
        }
    }
    if let Some(props) = schema.get("properties").and_then(JsonValue::as_object) {
        let disallow_extra = schema
            .get("additionalProperties")
            .and_then(JsonValue::as_bool)
            == Some(false);
        if disallow_extra {
            for k in args.keys() {
                if !props.contains_key(k) {
                    let qual = if prefix.is_empty() {
                        k.clone()
                    } else {
                        format!("{prefix}.{k}")
                    };
                    return Err(format!("unexpected_arg:{qual}"));
                }
            }
        }
        for (k, v) in args {
            if let Some(spec) = props.get(k).and_then(JsonValue::as_object) {
                let qual = if prefix.is_empty() {
                    k.clone()
                } else {
                    format!("{prefix}.{k}")
                };
                validate_field_schema(&qual, v, spec, depth + 1)?;
            }
        }
    }
    Ok(())
}

fn validate_field_schema(
    name: &str,
    val: &JsonValue,
    spec: &JsonObject,
    depth: usize,
) -> Result<(), String> {
    if depth > 16 {
        return Err("schema_depth_exceeded".to_owned());
    }
    let Some(ptype) = spec.get("type").and_then(JsonValue::as_str) else {
        return Ok(());
    };
    match ptype {
        "string" => {
            let Some(s) = val.as_str() else {
                return Err(format!("invalid_type:{name}:expected_string"));
            };
            let char_len = s.chars().count() as i64;
            if let Some(min_l) = spec.get("minLength").and_then(JsonValue::as_i64) {
                if char_len < min_l {
                    return Err(format!("min_length:{name}"));
                }
            }
            if let Some(max_l) = spec.get("maxLength").and_then(JsonValue::as_i64) {
                if char_len > max_l {
                    return Err(format!("max_length:{name}"));
                }
            }
            if let Some(enums) = spec.get("enum").and_then(JsonValue::as_array) {
                if !enums.iter().any(|e| e == val) {
                    return Err(format!("invalid_enum:{name}"));
                }
            }
        }
        "boolean" => {
            if val.as_bool().is_none() {
                return Err(format!("invalid_type:{name}:expected_boolean"));
            }
        }
        "integer" => {
            let Some(iv) = val.as_i64() else {
                return Err(format!("invalid_type:{name}:expected_integer"));
            };
            check_numeric_bounds(name, iv as f64, spec)?;
        }
        "number" => {
            let Some(fv) = val.as_f64() else {
                return Err(format!("invalid_type:{name}:expected_number"));
            };
            if !fv.is_finite() {
                return Err(format!("invalid_number:{name}"));
            }
            check_numeric_bounds(name, fv, spec)?;
        }
        "array" => {
            let Some(arr) = val.as_array() else {
                return Err(format!("invalid_type:{name}:expected_array"));
            };
            let len = arr.len() as i64;
            if let Some(min_i) = spec.get("minItems").and_then(JsonValue::as_i64) {
                if len < min_i {
                    return Err(format!("min_items:{name}"));
                }
            }
            if let Some(max_i) = spec.get("maxItems").and_then(JsonValue::as_i64) {
                if len > max_i {
                    return Err(format!("max_items:{name}"));
                }
            }
            if let Some(item_spec) = spec.get("items").and_then(JsonValue::as_object) {
                for (idx, elem) in arr.iter().enumerate() {
                    validate_field_schema(&format!("{name}[{idx}]"), elem, item_spec, depth + 1)?;
                }
            }
        }
        "object" => {
            let Some(sub_obj) = val.as_object() else {
                return Err(format!("invalid_type:{name}:expected_object"));
            };
            validate_object_schema(sub_obj, spec, name, depth + 1)?;
        }
        _ => {}
    }
    Ok(())
}

fn check_numeric_bounds(name: &str, v: f64, spec: &JsonObject) -> Result<(), String> {
    if let Some(min_v) = spec.get("minimum").and_then(JsonValue::as_f64) {
        if v < min_v {
            return Err(format!("below_minimum:{name}"));
        }
    }
    if let Some(max_v) = spec.get("maximum").and_then(JsonValue::as_f64) {
        if v > max_v {
            return Err(format!("above_maximum:{name}"));
        }
    }
    if let Some(ex_min) = spec.get("exclusiveMinimum").and_then(JsonValue::as_f64) {
        if v <= ex_min {
            return Err(format!("below_exclusive_minimum:{name}"));
        }
    }
    if let Some(ex_max) = spec.get("exclusiveMaximum").and_then(JsonValue::as_f64) {
        if v >= ex_max {
            return Err(format!("above_exclusive_maximum:{name}"));
        }
    }
    Ok(())
}

pub type TransportFn = Box<dyn Fn(&str, &JsonValue) + Send + Sync + 'static>;
pub type FallibleTransportFn =
    Box<dyn Fn(&str, &JsonValue) -> Result<(), String> + Send + Sync + 'static>;
pub type DomainEffectHandler =
    Box<dyn Fn(&str, &JsonObject, &str) -> EffectOutcome + Send + Sync + 'static>;

struct RegCap {
    def: CapabilityEntry,
    handler: DomainEffectHandler,
}

struct StateModule {
    snapshot_fn: Box<dyn Fn() -> JsonObject + Send + Sync + 'static>,
    grants_fn: Box<dyn Fn() -> Vec<ActiveMethodGrant> + Send + Sync + 'static>,
}

struct InnerState {
    current_epoch: i64,
    current_rev: i64,
    sync_enabled: bool,
    capabilities: BTreeMap<String, RegCap>,
    actors: BTreeMap<String, bool>,
    modules: BTreeMap<String, StateModule>,
    extensions_data: BTreeMap<String, JsonObject>,
    in_flight: BTreeMap<String, Box<dyn FnOnce() + Send + 'static>>,
    ledger: BTreeMap<String, OriginExecuteReply>,
    ledger_order: VecDeque<String>,
    pending_settled: Vec<OriginExecuteReply>,
    last_sent_methods: BTreeMap<(String, String), String>,
    last_brief_digest: String,
    last_cap_payload: Option<JsonValue>,
    visual_ref: Option<JsonObject>,
    audio_ref: Option<JsonObject>,
    allowed_timeline_tags: Option<Vec<String>>,
    metrics: OriginMetrics,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct OriginMetrics {
    pub frames_synced: u64,
    pub zero_change_skips: u64,
    pub steps_executed: u64,
    pub steps_settled_batch: u64,
    pub steps_cancelled: u64,
}

pub struct OriginSdk {
    slot_id: String,
    role_kind: String,
    billing_mode: String,
    room_id: String,
    faction_id: String,
    transport: FallibleTransportFn,
    chat_handler: Option<Box<dyn Fn(&str, &str) + Send + Sync + 'static>>,
    state: Mutex<InnerState>,
}

impl OriginSdk {
    pub fn new(
        slot_id: impl Into<String>,
        role_kind: impl Into<String>,
        billing_mode: impl Into<String>,
        room_id: impl Into<String>,
        faction_id: impl Into<String>,
        transport: TransportFn,
    ) -> Self {
        Self::new_fallible(
            slot_id,
            role_kind,
            billing_mode,
            room_id,
            faction_id,
            Box::new(move |op, payload| {
                transport(op, payload);
                Ok(())
            }),
        )
    }

    pub fn new_fallible(
        slot_id: impl Into<String>,
        role_kind: impl Into<String>,
        billing_mode: impl Into<String>,
        room_id: impl Into<String>,
        faction_id: impl Into<String>,
        transport: FallibleTransportFn,
    ) -> Self {
        Self {
            slot_id: slot_id.into(),
            role_kind: role_kind.into(),
            billing_mode: billing_mode.into(),
            room_id: room_id.into(),
            faction_id: faction_id.into(),
            transport,
            chat_handler: None,
            state: Mutex::new(InnerState {
                current_epoch: 1,
                current_rev: 0,
                sync_enabled: true,
                capabilities: BTreeMap::new(),
                actors: BTreeMap::new(),
                modules: BTreeMap::new(),
                extensions_data: BTreeMap::new(),
                in_flight: BTreeMap::new(),
                ledger: BTreeMap::new(),
                ledger_order: VecDeque::new(),
                pending_settled: Vec::new(),
                last_sent_methods: BTreeMap::new(),
                last_brief_digest: String::new(),
                last_cap_payload: None,
                visual_ref: None,
                audio_ref: None,
                allowed_timeline_tags: None,
                metrics: OriginMetrics::default(),
            }),
        }
    }

    pub fn with_chat_handler(
        mut self,
        handler: impl Fn(&str, &str) + Send + Sync + 'static,
    ) -> Self {
        self.chat_handler = Some(Box::new(handler));
        self
    }

    pub fn register_capability(&self, mut def: CapabilityEntry, handler: DomainEffectHandler) {
        if self.role_kind == "independent_npc" {
            def.permitted_actors.retain(|a| a != "player");
        }
        let mut st = self.state.lock().unwrap();
        st.capabilities.insert(def.method_id.clone(), RegCap { def, handler });
    }

    pub fn get_capability_actors(&self, method_id: &str) -> Option<Vec<String>> {
        let st = self.state.lock().unwrap();
        st.capabilities.get(method_id).map(|r| r.def.permitted_actors.clone())
    }

    pub fn bind_actor_subject(&self, actor: &str) {
        let mut st = self.state.lock().unwrap();
        st.actors.insert(actor.to_owned(), true);
    }

    pub fn bind_state_module(
        &self,
        name: &str,
        snapshot_fn: impl Fn() -> JsonObject + Send + Sync + 'static,
        grants_fn: impl Fn() -> Vec<ActiveMethodGrant> + Send + Sync + 'static,
    ) {
        let mut st = self.state.lock().unwrap();
        st.modules.insert(
            name.to_owned(),
            StateModule {
                snapshot_fn: Box::new(snapshot_fn),
                grants_fn: Box::new(grants_fn),
            },
        );
    }

    pub fn attach_extension_data(&self, extension_name: &str, data: JsonObject, flush: bool) {
        {
            let mut st = self.state.lock().unwrap();
            st.extensions_data.insert(extension_name.to_owned(), data);
        }
        if flush {
            self.flush_dirty_sync(false);
        }
    }

    pub fn attach_visual_frame(&self, visual_ref: JsonObject, flush: bool) {
        {
            let mut st = self.state.lock().unwrap();
            st.visual_ref = Some(visual_ref);
        }
        if flush {
            self.flush_dirty_sync(false);
        }
    }

    pub fn attach_audio_segment(&self, audio_ref: JsonObject, flush: bool) {
        {
            let mut st = self.state.lock().unwrap();
            st.audio_ref = Some(audio_ref);
        }
        if flush {
            self.flush_dirty_sync(false);
        }
    }

    pub fn set_allowed_timeline_tags(&self, tags: Vec<String>, flush: bool) {
        {
            let mut st = self.state.lock().unwrap();
            st.allowed_timeline_tags = Some(tags);
        }
        if flush {
            self.flush_dirty_sync(true);
        }
    }

    pub fn metrics(&self) -> OriginMetrics {
        self.state.lock().unwrap().metrics
    }

    pub fn publish_capabilities_and_baseline(&self, opts: SlotPublishOptions) {
        let cap_payload = {
            let st = self.state.lock().unwrap();
            let defs: JsonArray = st.capabilities.values().map(|r| r.def.to_json()).collect();
            let cap_digest = digest_sha256("g2a.cap.v1", &JsonValue::Array(defs.clone()))
                .expect("Capability JCS digest failed");
            let mut o = JsonObject::new();
            o.insert("slot_id".to_owned(), JsonValue::from(self.slot_id.as_str()));
            o.insert("role_kind".to_owned(), JsonValue::from(self.role_kind.as_str()));
            o.insert("billing_mode".to_owned(), JsonValue::from(self.billing_mode.as_str()));
            o.insert("origin_execute_url".to_owned(), JsonValue::from(opts.origin_execute_url.as_str()));
            o.insert("capability_table_digest".to_owned(), JsonValue::from(cap_digest));
            o.insert("capabilities".to_owned(), JsonValue::Array(defs));
            if opts.disabled {
                o.insert("disabled".to_owned(), JsonValue::from(true));
            }
            if opts.max_sessions > 0 {
                o.insert("max_sessions".to_owned(), JsonValue::from(opts.max_sessions));
            }
            if opts.shared_cooldown_ms > 0 {
                o.insert("shared_cooldown_ms".to_owned(), JsonValue::from(opts.shared_cooldown_ms));
            }
            if !opts.allowlisted_cores.is_empty() {
                o.insert(
                    "allowlisted_cores".to_owned(),
                    JsonValue::Array(opts.allowlisted_cores.iter().map(|c| JsonValue::from(c.as_str())).collect()),
                );
            }
            if !opts.allowlist_criteria_url.is_empty() {
                o.insert("allowlist_criteria_url".to_owned(), JsonValue::from(opts.allowlist_criteria_url.as_str()));
            }
            if !self.room_id.is_empty() {
                o.insert("room_id".to_owned(), JsonValue::from(self.room_id.as_str()));
            }
            if !self.faction_id.is_empty() {
                o.insert("faction_id".to_owned(), JsonValue::from(self.faction_id.as_str()));
            }
            let payload = JsonValue::Object(o);
            drop(st);
            self.state.lock().unwrap().last_cap_payload = Some(payload.clone());
            payload
        };
        let _ = (self.transport)("origin.capabilities", &cap_payload);
        self.flush_full_sync();
    }

    pub fn flush_dirty_sync(&self, significant_event: bool) -> bool {
        let maybe_prep = {
            let mut st = self.state.lock().unwrap();
            if !st.sync_enabled && st.pending_settled.is_empty() {
                return false;
            }
            let (brief, grants) = collect_view(&st);
            let ext_json = build_ext_json(&st.extensions_data);
            let digest_target = build_digest_target(&st, &brief, &ext_json);
            let brief_digest = digest_sha256("g2a.brief.v1", &digest_target).unwrap_or_default();

            let mut cur_canon = BTreeMap::new();
            let mut upserted = JsonArray::new();
            let mut removed = JsonArray::new();
            for (key, g) in &grants {
                let g_val = g.to_json();
                let canon = canonicalize_utf8(&g_val).unwrap_or_default();
                if st.last_sent_methods.get(key) != Some(&canon) {
                    upserted.push(g_val);
                }
                cur_canon.insert(key.clone(), canon);
            }
            for old_key in st.last_sent_methods.keys() {
                if !cur_canon.contains_key(old_key) {
                    let mut rem = JsonObject::new();
                    rem.insert("method_id".to_owned(), JsonValue::from(old_key.0.as_str()));
                    rem.insert("actor".to_owned(), JsonValue::from(old_key.1.as_str()));
                    removed.push(JsonValue::Object(rem));
                }
            }
            let settled_backup: Vec<OriginExecuteReply> = st.pending_settled.drain(..).collect();
            let settled: JsonArray = settled_backup.iter().map(|r| r.to_json()).collect();

            if brief_digest == st.last_brief_digest
                && upserted.is_empty()
                && removed.is_empty()
                && settled.is_empty()
            {
                st.metrics.zero_change_skips += 1;
                return false;
            }

            let prev_rev = st.current_rev;
            let prev_digest = st.last_brief_digest.clone();
            let prev_methods = st.last_sent_methods.clone();
            st.current_rev += 1;
            st.metrics.frames_synced += 1;
            st.last_brief_digest = brief_digest;
            st.last_sent_methods = cur_canon;

            let mut frame = JsonObject::new();
            frame.insert("slot_id".to_owned(), JsonValue::from(self.slot_id.as_str()));
            frame.insert("epoch".to_owned(), JsonValue::from(st.current_epoch));
            frame.insert("rev".to_owned(), JsonValue::from(st.current_rev));
            frame.insert("base_rev".to_owned(), JsonValue::from(prev_rev));
            frame.insert("mode".to_owned(), JsonValue::from("delta"));
            frame.insert("significant_event".to_owned(), JsonValue::from(significant_event));
            frame.insert("state_brief".to_owned(), JsonValue::Object(brief));
            frame.insert("upserted_methods".to_owned(), JsonValue::Array(upserted));
            frame.insert("removed_methods".to_owned(), JsonValue::Array(removed));
            frame.insert("settled_executions".to_owned(), JsonValue::Array(settled));
            append_optional_sync_fields(&st, &mut frame, ext_json);
            Some((
                JsonValue::Object(frame),
                settled_backup,
                prev_rev,
                prev_digest,
                prev_methods,
                st.last_cap_payload.clone(),
            ))
        };
        if let Some((frame, mut settled_backup, prev_rev, prev_digest, prev_methods, maybe_cap)) = maybe_prep {
            if let Err(err) = (self.transport)("origin.sync", &frame) {
                {
                    let mut st = self.state.lock().unwrap();
                    if !settled_backup.is_empty() {
                        settled_backup.append(&mut st.pending_settled);
                        st.pending_settled = settled_backup;
                    }
                    st.current_rev = prev_rev;
                    st.last_brief_digest = prev_digest;
                    st.last_sent_methods = prev_methods;
                }
                if err.contains("stale_context") || err.contains("HTTP 409") || err.contains("not_found") {
                    if let Some(cap_pl) = maybe_cap {
                        let _ = (self.transport)("origin.capabilities", &cap_pl);
                    }
                    self.flush_full_sync();
                    return true;
                }
                return false;
            }
            true
        } else {
            false
        }
    }

    pub fn flush_full_sync(&self) {
        let frame = {
            let mut st = self.state.lock().unwrap();
            let (brief, grants) = collect_view(&st);
            let ext_json = build_ext_json(&st.extensions_data);
            let digest_target = build_digest_target(&st, &brief, &ext_json);
            st.current_rev += 1;
            st.metrics.frames_synced += 1;
            st.last_brief_digest = digest_sha256("g2a.brief.v1", &digest_target).unwrap_or_default();
            st.last_sent_methods.clear();
            let mut upserted = JsonArray::new();
            for (key, g) in &grants {
                let g_val = g.to_json();
                st.last_sent_methods
                    .insert(key.clone(), canonicalize_utf8(&g_val).unwrap_or_default());
                upserted.push(g_val);
            }
            let settled: JsonArray = st.pending_settled.drain(..).map(|r| r.to_json()).collect();

            let mut frame = JsonObject::new();
            frame.insert("slot_id".to_owned(), JsonValue::from(self.slot_id.as_str()));
            frame.insert("epoch".to_owned(), JsonValue::from(st.current_epoch));
            frame.insert("rev".to_owned(), JsonValue::from(st.current_rev));
            frame.insert("mode".to_owned(), JsonValue::from("full"));
            frame.insert("significant_event".to_owned(), JsonValue::from(true));
            frame.insert("state_brief".to_owned(), JsonValue::Object(brief));
            frame.insert("upserted_methods".to_owned(), JsonValue::Array(upserted));
            frame.insert("removed_methods".to_owned(), JsonValue::Array(JsonArray::new()));
            frame.insert("settled_executions".to_owned(), JsonValue::Array(settled));
            append_optional_sync_fields(&st, &mut frame, ext_json);
            JsonValue::Object(frame)
        };
        let _ = (self.transport)("origin.sync", &frame);
    }

    pub fn handle_origin_execute(&self, req: &OriginExecuteRequest) -> OriginExecuteReply {
        if req.kind == "cancel" {
            return self.cancel_in_flight_step(&req.execution_id);
        }
        if req.kind == "deliver_chat" {
            let aud = req.args.get("audience").and_then(JsonValue::as_str).unwrap_or("player_only");
            let txt = req.args.get("text").and_then(JsonValue::as_str).unwrap_or("");
            if let Some(ref cb) = self.chat_handler {
                cb(aud, txt);
            }
            return OriginExecuteReply {
                execution_id: req.execution_id.clone(),
                state: "succeeded".to_owned(),
                effect_summary: None,
                failure_reason: None,
            };
        }

        let mut should_sync = false;
        let reply = {
            let mut st = self.state.lock().unwrap();
            if let Some(prev) = st.ledger.get(&req.execution_id) {
                return prev.clone();
            }
            if req.epoch != st.current_epoch {
                return OriginExecuteReply {
                    execution_id: req.execution_id.clone(),
                    state: "failed".to_owned(),
                    effect_summary: None,
                    failure_reason: Some("stale_epoch".to_owned()),
                };
            }
            let (Some(mid), Some(act)) = (req.method_id.as_deref(), req.actor.as_deref()) else {
                return OriginExecuteReply {
                    execution_id: req.execution_id.clone(),
                    state: "failed".to_owned(),
                    effect_summary: None,
                    failure_reason: Some("missing_method_or_actor".to_owned()),
                };
            };
            if !st.actors.contains_key(act) {
                return OriginExecuteReply {
                    execution_id: req.execution_id.clone(),
                    state: "failed".to_owned(),
                    effect_summary: None,
                    failure_reason: Some("unknown_actor".to_owned()),
                };
            }
            let Some(reg) = st.capabilities.get(mid) else {
                return OriginExecuteReply {
                    execution_id: req.execution_id.clone(),
                    state: "failed".to_owned(),
                    effect_summary: None,
                    failure_reason: Some("unknown_method".to_owned()),
                };
            };
            if !reg.def.permitted_actors.iter().any(|a| a == act) {
                return OriginExecuteReply {
                    execution_id: req.execution_id.clone(),
                    state: "failed".to_owned(),
                    effect_summary: None,
                    failure_reason: Some("permission_denied".to_owned()),
                };
            }
            if let Err(schema_err) = validate_args_against_schema(&req.args, &reg.def.params_schema) {
                return OriginExecuteReply {
                    execution_id: req.execution_id.clone(),
                    state: "failed".to_owned(),
                    effect_summary: None,
                    failure_reason: Some(schema_err),
                };
            }

            let out = (reg.handler)(act, &req.args, &req.execution_id);
            st.metrics.steps_executed += 1;
            match out {
                EffectOutcome::StartedExecuting(cancel_fn) => {
                    st.in_flight.insert(req.execution_id.clone(), cancel_fn);
                    let rep = OriginExecuteReply {
                        execution_id: req.execution_id.clone(),
                        state: "executing".to_owned(),
                        effect_summary: None,
                        failure_reason: None,
                    };
                    store_ledger_reply(&mut st, rep.clone());
                    rep
                }
                EffectOutcome::ImmediateSucceeded(sum) => {
                    should_sync = true;
                    let rep = OriginExecuteReply {
                        execution_id: req.execution_id.clone(),
                        state: "succeeded".to_owned(),
                        effect_summary: Some(sum),
                        failure_reason: None,
                    };
                    store_ledger_reply(&mut st, rep.clone());
                    rep
                }
                EffectOutcome::ImmediateFailed(reason) => {
                    should_sync = true;
                    let rep = OriginExecuteReply {
                        execution_id: req.execution_id.clone(),
                        state: "failed".to_owned(),
                        effect_summary: None,
                        failure_reason: Some(reason),
                    };
                    store_ledger_reply(&mut st, rep.clone());
                    rep
                }
            }
        };
        if should_sync {
            self.flush_dirty_sync(false);
        }
        reply
    }

    pub fn complete_step(&self, execution_id: &str, succeeded: bool, summary: Option<JsonObject>, reason: Option<String>) {
        let _ = self.complete_steps_batch(&[(execution_id.to_owned(), succeeded, summary, reason)]);
    }

    /// Atomically settles multiple sustained steps in one lock scope and emits at most one `origin.sync` delta frame.
    pub fn complete_steps_batch(
        &self,
        settlements: &[(String, bool, Option<JsonObject>, Option<String>)],
    ) -> usize {
        let count = {
            let mut st = self.state.lock().unwrap();
            let mut n = 0usize;
            for (execution_id, succeeded, summary, reason) in settlements {
                if st.in_flight.remove(execution_id).is_none() {
                    continue;
                }
                let rep = OriginExecuteReply {
                    execution_id: execution_id.clone(),
                    state: if *succeeded { "succeeded".to_owned() } else { "failed".to_owned() },
                    effect_summary: if *succeeded { summary.clone() } else { None },
                    failure_reason: if *succeeded { None } else { reason.clone() },
                };
                store_ledger_reply(&mut st, rep.clone());
                st.pending_settled.push(rep);
                n += 1;
            }
            st.metrics.steps_settled_batch += n as u64;
            n
        };
        if count > 0 {
            self.flush_dirty_sync(false);
        }
        count
    }

    pub fn cancel_in_flight_step(&self, execution_id: &str) -> OriginExecuteReply {
        let (cancel_cb, rep, should_sync) = {
            let mut st = self.state.lock().unwrap();
            if let Some(cb) = st.in_flight.remove(execution_id) {
                let r = OriginExecuteReply {
                    execution_id: execution_id.to_owned(),
                    state: "cancelled".to_owned(),
                    effect_summary: None,
                    failure_reason: Some("cancelled_by_hub".to_owned()),
                };
                store_ledger_reply(&mut st, r.clone());
                st.pending_settled.push(r.clone());
                st.metrics.steps_cancelled += 1;
                (Some(cb), r, true)
            } else if let Some(prev) = st.ledger.get(execution_id) {
                (None, prev.clone(), false)
            } else {
                (
                    None,
                    OriginExecuteReply {
                        execution_id: execution_id.to_owned(),
                        state: "cancelled".to_owned(),
                        effect_summary: None,
                        failure_reason: None,
                    },
                    false,
                )
            }
        };
        if let Some(cb) = cancel_cb {
            cb();
        }
        if should_sync {
            self.flush_dirty_sync(false);
        }
        rep
    }

    pub fn advance_epoch(&self) {
        let callbacks = {
            let mut st = self.state.lock().unwrap();
            st.current_epoch += 1;
            st.current_rev = 0;
            st.last_sent_methods.clear();
            drain_all_in_flight(&mut st, "epoch_advanced")
        };
        for cb in callbacks {
            cb();
        }
        self.flush_full_sync();
    }

    pub fn report_player_command(&self, text: &str) {
        self.flush_dirty_sync(true);
        let mut o = JsonObject::new();
        o.insert("slot_id".to_owned(), JsonValue::from(self.slot_id.as_str()));
        o.insert("kind".to_owned(), JsonValue::from("player_command"));
        o.insert("text".to_owned(), JsonValue::from(text));
        let _ = (self.transport)("origin.player_event", &JsonValue::Object(o));
    }

    pub fn report_player_presence(&self, online: bool, remaining_online_humans: i64) {
        {
            let mut st = self.state.lock().unwrap();
            st.sync_enabled = online || remaining_online_humans > 0;
        }
        let mut o = JsonObject::new();
        o.insert("slot_id".to_owned(), JsonValue::from(self.slot_id.as_str()));
        o.insert("kind".to_owned(), JsonValue::from("presence_changed"));
        o.insert("online".to_owned(), JsonValue::from(online));
        o.insert("remaining_online_humans".to_owned(), JsonValue::from(remaining_online_humans));
        let _ = (self.transport)("origin.player_event", &JsonValue::Object(o));
    }

    pub fn report_emergency_stop(&self) {
        let callbacks = {
            let mut st = self.state.lock().unwrap();
            drain_all_in_flight(&mut st, "player_emergency_stop")
        };
        for cb in callbacks {
            cb();
        }
        self.flush_dirty_sync(false);
        let mut o = JsonObject::new();
        o.insert("slot_id".to_owned(), JsonValue::from(self.slot_id.as_str()));
        o.insert("kind".to_owned(), JsonValue::from("emergency_stop"));
        let _ = (self.transport)("origin.player_event", &JsonValue::Object(o));
    }

    pub fn report_queue_approval(
        &self,
        session_id: &str,
        queue_id: &str,
        queue_digest: &str,
        fingerprint: &str,
        approved: bool,
    ) {
        let mut o = JsonObject::new();
        o.insert("slot_id".to_owned(), JsonValue::from(self.slot_id.as_str()));
        o.insert("kind".to_owned(), JsonValue::from("queue_approval"));
        o.insert("session_id".to_owned(), JsonValue::from(session_id));
        o.insert("queue_id".to_owned(), JsonValue::from(queue_id));
        o.insert("queue_digest".to_owned(), JsonValue::from(queue_digest));
        o.insert("fingerprint".to_owned(), JsonValue::from(fingerprint));
        o.insert("approved".to_owned(), JsonValue::from(approved));
        let _ = (self.transport)("origin.player_event", &JsonValue::Object(o));
    }

    pub fn report_npc_host_handoff(&self, new_player_id: &str) -> Result<(), String> {
        let cleaned = new_player_id.trim();
        if cleaned.is_empty() {
            return Err("new_player_id is required".to_owned());
        }
        if self.role_kind != "independent_npc" || self.billing_mode != "player_metered" {
            return Err("host_handoff requires player_metered independent_npc slot".to_owned());
        }
        let callbacks = {
            let mut st = self.state.lock().unwrap();
            st.sync_enabled = true;
            drain_all_in_flight(&mut st, "npc_host_handoff")
        };
        for cb in callbacks {
            cb();
        }
        self.flush_dirty_sync(false);
        let mut o = JsonObject::new();
        o.insert("slot_id".to_owned(), JsonValue::from(self.slot_id.as_str()));
        o.insert("kind".to_owned(), JsonValue::from("host_handoff"));
        o.insert("new_player_id".to_owned(), JsonValue::from(cleaned));
        let _ = (self.transport)("origin.player_event", &JsonValue::Object(o));
        Ok(())
    }
}

fn build_ext_json(ext: &BTreeMap<String, JsonObject>) -> JsonObject {
    let mut out = JsonObject::new();
    for (k, v) in ext {
        out.insert(k.clone(), JsonValue::Object(v.clone()));
    }
    out
}

fn build_digest_target(st: &InnerState, brief: &JsonObject, ext_json: &JsonObject) -> JsonValue {
    if ext_json.is_empty()
        && st.visual_ref.is_none()
        && st.audio_ref.is_none()
        && st.allowed_timeline_tags.is_none()
    {
        return JsonValue::Object(brief.clone());
    }
    let mut comb = JsonObject::new();
    comb.insert("brief".to_owned(), JsonValue::Object(brief.clone()));
    if !ext_json.is_empty() {
        comb.insert("ext".to_owned(), JsonValue::Object(ext_json.clone()));
    }
    if let Some(ref v) = st.visual_ref {
        comb.insert("visual".to_owned(), JsonValue::Object(v.clone()));
    }
    if let Some(ref a) = st.audio_ref {
        comb.insert("audio".to_owned(), JsonValue::Object(a.clone()));
    }
    if let Some(ref tags) = st.allowed_timeline_tags {
        comb.insert(
            "timelines".to_owned(),
            JsonValue::Array(tags.iter().map(|t| JsonValue::from(t.as_str())).collect()),
        );
    }
    JsonValue::Object(comb)
}

fn append_optional_sync_fields(st: &InnerState, frame: &mut JsonObject, ext_json: JsonObject) {
    if let Some(ref v) = st.visual_ref {
        frame.insert("visual_ref".to_owned(), JsonValue::Object(v.clone()));
    }
    if let Some(ref a) = st.audio_ref {
        frame.insert("audio_ref".to_owned(), JsonValue::Object(a.clone()));
    }
    if let Some(ref tags) = st.allowed_timeline_tags {
        frame.insert(
            "allowed_timeline_tags".to_owned(),
            JsonValue::Array(tags.iter().map(|t| JsonValue::from(t.as_str())).collect()),
        );
    }
    if !ext_json.is_empty() {
        frame.insert("extensions_data".to_owned(), JsonValue::Object(ext_json));
    }
}

fn store_ledger_reply(st: &mut InnerState, rep: OriginExecuteReply) {
    if !st.ledger.contains_key(&rep.execution_id) {
        st.ledger_order.push_back(rep.execution_id.clone());
    }
    st.ledger.insert(rep.execution_id.clone(), rep);
    let mut guard = st.ledger_order.len();
    while st.ledger.len() > MAX_EXECUTION_LEDGER && guard > 0 {
        guard -= 1;
        if let Some(oldest) = st.ledger_order.pop_front() {
            if st.in_flight.contains_key(&oldest) {
                st.ledger_order.push_back(oldest);
            } else {
                st.ledger.remove(&oldest);
            }
        }
    }
}

fn drain_all_in_flight(st: &mut InnerState, reason: &str) -> Vec<Box<dyn FnOnce() + Send + 'static>> {
    let active: Vec<(String, Box<dyn FnOnce() + Send + 'static>)> =
        std::mem::take(&mut st.in_flight).into_iter().collect();
    let mut callbacks = Vec::with_capacity(active.len());
    for (exec_id, cb) in active {
        callbacks.push(cb);
        let rep = OriginExecuteReply {
            execution_id: exec_id,
            state: "cancelled".to_owned(),
            effect_summary: None,
            failure_reason: Some(reason.to_owned()),
        };
        store_ledger_reply(st, rep.clone());
        st.pending_settled.push(rep);
    }
    callbacks
}

fn parse_anchor_meters(anchor: &Option<String>) -> f64 {
    let Some(s) = anchor.as_deref() else {
        return 1e9;
    };
    let trimmed = s.trim();
    let Some(without_m) = trimmed.strip_suffix('m') else {
        return 1e9;
    };
    let tok = without_m.rsplit(' ').next().unwrap_or(without_m);
    tok.parse::<f64>().ok().filter(|d| d.is_finite()).unwrap_or(1e9)
}

fn prioritize_targets(raw: &[TargetCandidate]) -> Vec<TargetCandidate> {
    let mut dedup: BTreeMap<String, TargetCandidate> = BTreeMap::new();
    for t in raw {
        let replace = match dedup.get(&t.target_id) {
            None => true,
            Some(_) => t.readiness == "ready",
        };
        if replace {
            dedup.insert(t.target_id.clone(), t.clone());
        }
    }
    let mut ordered: Vec<TargetCandidate> = dedup.into_values().collect();
    ordered.sort_by(|a, b| {
        let a_al = if a.alert_tag.as_deref().unwrap_or("").is_empty() { 1 } else { 0 };
        let b_al = if b.alert_tag.as_deref().unwrap_or("").is_empty() { 1 } else { 0 };
        if a_al != b_al {
            return a_al.cmp(&b_al);
        }
        let a_pt = if a.player_anchor.as_deref().unwrap_or("").to_ascii_lowercase().contains("pointed") { 0 } else { 1 };
        let b_pt = if b.player_anchor.as_deref().unwrap_or("").to_ascii_lowercase().contains("pointed") { 0 } else { 1 };
        if a_pt != b_pt {
            return a_pt.cmp(&b_pt);
        }
        let a_rdy = if a.readiness == "ready" { 0 } else { 1 };
        let b_rdy = if b.readiness == "ready" { 0 } else { 1 };
        if a_rdy != b_rdy {
            return a_rdy.cmp(&b_rdy);
        }
        let d_diff = parse_anchor_meters(&a.actor_anchor) - parse_anchor_meters(&b.actor_anchor);
        if d_diff.abs() > 1e-6 {
            return d_diff.partial_cmp(&0.0).unwrap_or(std::cmp::Ordering::Equal);
        }
        a.target_id.cmp(&b.target_id)
    });
    ordered.truncate(MAX_CANDIDATE_TARGETS);
    ordered
}

fn collect_view(st: &InnerState) -> (JsonObject, BTreeMap<(String, String), ActiveMethodGrant>) {
    let mut brief = JsonObject::new();
    let mut grants: BTreeMap<(String, String), ActiveMethodGrant> = BTreeMap::new();
    for (name, m) in &st.modules {
        brief.insert(name.clone(), JsonValue::Object((m.snapshot_fn)()));
        for g in (m.grants_fn)() {
            let key = (g.method_id.clone(), g.actor.clone());
            if let Some(prev) = grants.get_mut(&key) {
                let mut combined = prev.targets.clone();
                combined.extend(g.targets);
                let prioritized = prioritize_targets(&combined);
                let any_ready = prev.readiness == "ready"
                    || g.readiness == "ready"
                    || prioritized.iter().any(|t| t.readiness == "ready");
                *prev = ActiveMethodGrant {
                    method_id: g.method_id,
                    actor: g.actor,
                    readiness: if any_ready { "ready".to_owned() } else { "staged".to_owned() },
                    targets: prioritized,
                };
            } else {
                grants.insert(
                    key,
                    ActiveMethodGrant {
                        method_id: g.method_id,
                        actor: g.actor,
                        readiness: g.readiness,
                        targets: prioritize_targets(&g.targets),
                    },
                );
            }
        }
    }
    (brief, grants)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpStream;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use transport::LoopbackExecuteListener;

    fn read_one_http(reader: &mut BufReader<TcpStream>) -> (String, String) {
        let mut status = String::new();
        reader.read_line(&mut status).unwrap();
        let mut cl = 0usize;
        loop {
            let mut h = String::new();
            reader.read_line(&mut h).unwrap();
            let t = h.trim_end_matches(['\r', '\n']);
            if t.is_empty() {
                break;
            }
            if let Some((k, v)) = t.split_once(':') {
                if k.eq_ignore_ascii_case("content-length") {
                    cl = v.trim().parse().unwrap_or(0);
                }
            }
        }
        let mut body = vec![0u8; cl];
        reader.read_exact(&mut body).unwrap();
        (status, String::from_utf8(body).unwrap())
    }

    #[test]
    fn golden_jcs_sha256_and_utf16_surrogate_parity() {
        let mut args = JsonObject::new();
        args.insert("qty".to_owned(), JsonValue::from(20.0));
        args.insert("item".to_owned(), JsonValue::from("timber"));
        let mut step = JsonObject::new();
        step.insert("args".to_owned(), JsonValue::Object(args));
        step.insert("step_index".to_owned(), JsonValue::from(0i64));
        let mut doc = JsonObject::new();
        doc.insert("steps".to_owned(), JsonValue::Array(vec![JsonValue::Object(step)]));
        doc.insert("queue_id".to_owned(), JsonValue::from("Q-205"));
        doc.insert("session_id".to_owned(), JsonValue::from("sess-1"));

        let d = digest_sha256("g2a.confirm.v1", &JsonValue::Object(doc)).unwrap();
        assert_eq!(
            d,
            "sha256:8d8634fd72ae48e655a9151e50b06a3e1095e753cacb2c293706e59af1b9ccd8"
        );

        // Duplicate key & lone surrogate rejection
        assert!(jcs::parse_strict_json(r#"{"a":1,"a":2}"#).is_err());
        assert!(jcs::parse_strict_json(r#"{"bad":"\uD83D"}"#).is_err());

        // UTF-16 code-unit order:😀 (0xD83D 0xDE00) sorts BEFORE U+E000 (0xE000)!
        let parsed = jcs::parse_strict_json(r#"{"\uE000":"bmp","\uD83D\uDE00":"astral"}"#).unwrap();
        assert_eq!(canonicalize_utf8(&parsed).unwrap(), "{\"😀\":\"astral\",\"\u{E000}\":\"bmp\"}");
    }

    #[test]
    fn origin_sdk_crowd_sorting_schema_validation_extensions_and_keepalive_listener() {
        let posts: Arc<Mutex<Vec<(String, JsonValue)>>> = Arc::new(Mutex::new(Vec::new()));
        let posts_c = Arc::clone(&posts);
        let cancelled = Arc::new(AtomicBool::new(false));
        let cancelled_c = Arc::clone(&cancelled);

        let sdk = Arc::new(OriginSdk::new(
            "slot-rust-test",
            "companion",
            "player_metered",
            "room-1",
            "faction-blue",
            Box::new(move |op, payload| {
                posts_c.lock().unwrap().push((op.to_owned(), payload.clone()));
            }),
        ));

        sdk.bind_actor_subject("companion");
        let mut qty_spec = JsonObject::new();
        qty_spec.insert("type".to_owned(), JsonValue::from("integer"));
        qty_spec.insert("minimum".to_owned(), JsonValue::from(1i64));
        qty_spec.insert("maximum".to_owned(), JsonValue::from(50i64));
        let mut props = JsonObject::new();
        props.insert("qty".to_owned(), JsonValue::Object(qty_spec));
        let mut schema = JsonObject::new();
        schema.insert("type".to_owned(), JsonValue::from("object"));
        schema.insert("required".to_owned(), JsonValue::Array(vec![JsonValue::from("qty")]));
        schema.insert("additionalProperties".to_owned(), JsonValue::from(false));
        schema.insert("properties".to_owned(), JsonValue::Object(props));

        sdk.register_capability(
            CapabilityEntry {
                method_id: "#01".to_owned(),
                name: "haul_wood".to_owned(),
                description: "Haul wood".to_owned(),
                permitted_actors: vec!["companion".to_owned()],
                params_schema: schema,
                sustained: true,
                risk_flags: vec![],
                default_policy: "allow".to_owned(),
            },
            Box::new(move |_, _, _| {
                let c = Arc::clone(&cancelled_c);
                EffectOutcome::StartedExecuting(Box::new(move || c.store(true, Ordering::SeqCst)))
            }),
        );

        sdk.bind_state_module(
            "yard",
            || {
                let mut b = JsonObject::new();
                b.insert("stacks".to_owned(), JsonValue::from(3i64));
                b
            },
            || {
                let mut targets = Vec::new();
                for i in 0..35 {
                    targets.push(TargetCandidate {
                        target_id: format!("pile_{i:02}"),
                        label: "Wood Pile".to_owned(),
                        readiness: "staged".to_owned(),
                        prerequisite_hint: None,
                        actor_anchor: Some("18.0m".to_owned()),
                        player_anchor: None,
                        alert_tag: None,
                    });
                }
                targets.push(TargetCandidate {
                    target_id: "sawmill_alert".to_owned(),
                    label: "Halted Sawmill".to_owned(),
                    readiness: "staged".to_owned(),
                    prerequisite_hint: None,
                    actor_anchor: Some("12.0m".to_owned()),
                    player_anchor: None,
                    alert_tag: Some("halted_no_logs".to_owned()),
                });
                targets.push(TargetCandidate {
                    target_id: "pile_pointed".to_owned(),
                    label: "Pointed Pile".to_owned(),
                    readiness: "ready".to_owned(),
                    prerequisite_hint: None,
                    actor_anchor: Some("4.0m".to_owned()),
                    player_anchor: Some("pointed_ray 4.0m".to_owned()),
                    alert_tag: None,
                });
                vec![ActiveMethodGrant {
                    method_id: "#01".to_owned(),
                    actor: "companion".to_owned(),
                    readiness: "staged".to_owned(),
                    targets,
                }]
            },
        );

        let mut ext_payload = JsonObject::new();
        ext_payload.insert("blueprint".to_owned(), JsonValue::from("mill_v2"));
        sdk.attach_extension_data("org.g2a.sim.batch_blueprint.v1", ext_payload, false);

        sdk.publish_capabilities_and_baseline(SlotPublishOptions {
            origin_execute_url: transport::PULL_EXECUTE_SENTINEL.to_owned(),
            shared_cooldown_ms: 500,
            ..Default::default()
        });
        assert_eq!(posts.lock().unwrap().len(), 2);
        assert!(!sdk.flush_dirty_sync(false)); // Zero-change zero-packet!

        // Schema validation rejects out-of-range qty=999
        let mut bad_args = JsonObject::new();
        bad_args.insert("qty".to_owned(), JsonValue::from(999i64));
        let bad_rep = sdk.handle_origin_execute(&OriginExecuteRequest {
            slot_id: "slot-rust-test".to_owned(),
            epoch: 1,
            kind: "invoke".to_owned(),
            execution_id: "ex-bad".to_owned(),
            method_id: Some("#01".to_owned()),
            actor: Some("companion".to_owned()),
            args: bad_args,
        });
        assert_eq!(bad_rep.state, "failed");
        assert_eq!(bad_rep.failure_reason.as_deref(), Some("above_maximum:qty"));

        // Verify Pose3D 360-degree yaw-relative deictics
        let p_pose = Pose3D { x: 0.0, y: 0.0, z: 0.0, yaw_deg: 0.0 };
        let t_right = Pose3D { x: 3.0, y: 0.0, z: 0.0, yaw_deg: 0.0 };
        assert_eq!(p_pose.format_player_deictic_anchor(&t_right, false), "right 3.0m");

        // Verify live HTTP/1.1 Keep-Alive LoopbackExecuteListener + constant-time Bearer token check
        let listener = LoopbackExecuteListener::start(Arc::clone(&sdk), 0, "rs-secret").unwrap();
        let port: u16 = listener
            .execute_url()
            .strip_prefix("http://127.0.0.1:")
            .unwrap()
            .split('/')
            .next()
            .unwrap()
            .parse()
            .unwrap();
        let mut tcp = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let body = r##"{"g2a_version":"0.0.1","request_id":"r1","timestamp_ms":1,"operation":"origin.execute","payload":{"slot_id":"slot-rust-test","epoch":1,"kind":"invoke","execution_id":"ex-ka-1","method_id":"#01","actor":"companion","args":{"qty":10}},"extensions":{}}"##;
        let req1 = format!(
            "POST /g2a/v1/origin/execute HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer wrong\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        );
        tcp.write_all(req1.as_bytes()).unwrap();
        let mut reader = BufReader::new(tcp.try_clone().unwrap());
        let (st1, _) = read_one_http(&mut reader);
        assert!(st1.starts_with("HTTP/1.1 401"));

        // Reuse SAME Keep-Alive socket with valid token!
        let req2 = format!(
            "POST /g2a/v1/origin/execute HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer rs-secret\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        );
        tcp.write_all(req2.as_bytes()).unwrap();
        let (st2, body2) = read_one_http(&mut reader);
        assert!(st2.starts_with("HTTP/1.1 200"));
        assert!(body2.contains("\"executing\""));

        let c_rep = sdk.cancel_in_flight_step("ex-ka-1");
        assert_eq!(c_rep.state, "cancelled");
        assert!(cancelled.load(Ordering::SeqCst));

        // Verify complete_steps_batch coalesces multiple sustained settlements into a single origin.sync delta frame
        let mut ok_args = JsonObject::new();
        ok_args.insert("qty".to_owned(), JsonValue::from(5i64));
        for eid in ["ex-rs-b1", "ex-rs-b2"] {
            let r = sdk.handle_origin_execute(&OriginExecuteRequest {
                slot_id: "slot-rust-test".to_owned(),
                epoch: 1,
                kind: "invoke".to_owned(),
                execution_id: eid.to_owned(),
                method_id: Some("#01".to_owned()),
                actor: Some("companion".to_owned()),
                args: ok_args.clone(),
            });
            assert_eq!(r.state, "executing");
        }
        let posts_before = posts.lock().unwrap().len();
        let batch_n = sdk.complete_steps_batch(&[
            ("ex-rs-b1".to_owned(), true, None, None),
            ("ex-rs-b2".to_owned(), false, None, Some("cart_full".to_owned())),
        ]);
        assert_eq!(batch_n, 2);
        let locked_posts = posts.lock().unwrap();
        assert_eq!(locked_posts.len() - posts_before, 1);
        let last_sync_wire = jcs::canonicalize_utf8(&locked_posts.last().unwrap().1).unwrap();
        assert!(last_sync_wire.contains("\"ex-rs-b1\""));
        assert!(last_sync_wire.contains("\"ex-rs-b2\""));
        drop(locked_posts);

        // Verify multimodal refs, allowed_timeline_tags sync, & OriginMetrics
        let mut vref = JsonObject::new();
        vref.insert("frame_id".to_owned(), JsonValue::from("vf-rs-1"));
        sdk.attach_visual_frame(vref, false);
        sdk.set_allowed_timeline_tags(vec!["branch_rust".to_owned()], true);
        assert!(!sdk.flush_dirty_sync(false));
        let m = sdk.metrics();
        assert!(m.frames_synced >= 3 && m.zero_change_skips >= 2 && m.steps_settled_batch == 2 && m.steps_cancelled == 1);
    }

    #[test]
    fn origin_sdk_fallible_transport_resyncs_on_stale_context_preserving_settled() {
        use std::sync::atomic::AtomicUsize;
        use std::sync::{Arc, Mutex};

        // 0 = OK, 1 = transient 503 unavailable, 2 = reject next delta sync with 409 stale_context
        let mode = Arc::new(AtomicUsize::new(0));
        let mode_cl = Arc::clone(&mode);
        let posts: Arc<Mutex<Vec<(String, JsonValue)>>> = Arc::new(Mutex::new(Vec::new()));
        let posts_cl = Arc::clone(&posts);

        let sdk = OriginSdk::new_fallible(
            "slot-rust-resync",
            "companion",
            "player_byok",
            "room_1",
            "fac_1",
            Box::new(move |op, payload| {
                let cur = mode_cl.load(Ordering::SeqCst);
                if cur == 1 {
                    return Err("Hub HTTP 503: unavailable".to_owned());
                }
                if cur == 2 && op == "origin.sync" {
                    let is_delta = payload
                        .as_object()
                        .and_then(|o| o.get("mode"))
                        .and_then(JsonValue::as_str)
                        == Some("delta");
                    if is_delta {
                        mode_cl.store(0, Ordering::SeqCst);
                        return Err("Hub HTTP 409: stale_context (base_rev mismatch)".to_owned());
                    }
                }
                posts_cl.lock().unwrap().push((op.to_owned(), payload.clone()));
                Ok(())
            }),
        );

        sdk.bind_actor_subject("companion");
        sdk.register_capability(
            CapabilityEntry {
                method_id: "#01".to_owned(),
                name: "hold_valve".to_owned(),
                description: "Hold pressure valve".to_owned(),
                permitted_actors: vec!["companion".to_owned()],
                params_schema: JsonObject::new(),
                sustained: true,
                risk_flags: vec![],
                default_policy: "allow".to_owned(),
            },
            Box::new(|_, _, _| EffectOutcome::StartedExecuting(Box::new(|| {}))),
        );
        sdk.bind_state_module(
            "boiler",
            || {
                let mut m = JsonObject::new();
                m.insert("psi".to_owned(), JsonValue::from(120i64));
                m
            },
            || {
                vec![ActiveMethodGrant {
                    method_id: "#01".to_owned(),
                    actor: "companion".to_owned(),
                    readiness: "ready".to_owned(),
                    targets: Vec::new(),
                }]
            },
        );
        sdk.publish_capabilities_and_baseline(SlotPublishOptions::default());
        assert_eq!(sdk.state.lock().unwrap().current_rev, 1);

        let rep = sdk.handle_origin_execute(&OriginExecuteRequest {
            slot_id: "slot-rust-resync".to_owned(),
            epoch: 1,
            kind: "invoke".to_owned(),
            execution_id: "ex-fallible-1".to_owned(),
            method_id: Some("#01".to_owned()),
            actor: Some("companion".to_owned()),
            args: JsonObject::new(),
        });
        assert_eq!(rep.state, "executing");

        // 1. Transient 503 failure rolls back rev & preserves settled execution in pending_settled!
        mode.store(1, Ordering::SeqCst);
        sdk.complete_step("ex-fallible-1", true, None, None);
        assert_eq!(sdk.state.lock().unwrap().current_rev, 1);

        // 2. Subsequent delta sync encounters 409 stale_context -> auto-heals via origin.capabilities + full sync!
        mode.store(2, Ordering::SeqCst);
        assert!(sdk.flush_dirty_sync(true));

        let locked = posts.lock().unwrap();
        assert!(locked.len() >= 4);
        let resub_cap = &locked[locked.len() - 2];
        let resub_full = &locked[locked.len() - 1];
        assert_eq!(resub_cap.0, "origin.capabilities");
        assert_eq!(resub_full.0, "origin.sync");
        let full_wire = jcs::canonicalize_utf8(&resub_full.1).unwrap();
        assert!(full_wire.contains("\"mode\":\"full\""));
        assert!(full_wire.contains("\"ex-fallible-1\""));
        assert!(full_wire.contains("\"succeeded\""));
    }
}
