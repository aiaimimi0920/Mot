import assert from "node:assert/strict";
import {
  OriginSDK,
  canonicalizeJcs,
  formatPlayerDeicticAnchor,
  jcsSha256,
  parseStrictJson,
  validateArgsAgainstSchema,
} from "../esm/index.mjs";

const golden = "sha256:8d8634fd72ae48e655a9151e50b06a3e1095e753cacb2c293706e59af1b9ccd8";
const got = await jcsSha256("g2a.confirm.v1", {
  steps: [{ args: { qty: 20.0, item: "timber" }, step_index: 0 }],
  queue_id: "Q-205",
  session_id: "sess-1",
});
assert.equal(got, golden);

assert.throws(() => parseStrictJson('{"a":1,"a":2}'));
assert.throws(() => parseStrictJson('{"bad":"\\uD83D"}'));
assert.equal(
  canonicalizeJcs({ "\uE000": "bmp", "😀": "astral" }),
  '{"😀":"astral","\uE000":"bmp"}'
);

assert.equal(
  formatPlayerDeicticAnchor({ x: 0, y: 0, z: 0, yaw_deg: 0 }, { x: 3, y: 0, z: 0 }, false),
  "right 3.0m"
);
assert.equal(
  validateArgsAgainstSchema(
    { qty: 999 },
    { type: "object", required: ["qty"], properties: { qty: { type: "integer", minimum: 1, maximum: 50 } } }
  ),
  "above_maximum:qty"
);

// Verify independent_npc strips "player" actor & enforces required schema in handleOriginExecute
const npc = new OriginSDK("slot-ts-npc", "http://127.0.0.1:19210", "independent_npc", "player_metered");
npc.bindActorSubject("boss", {});
npc.registerCapability(
  {
    method_id: "#07",
    name: "roar",
    description: "Roar",
    permitted_actors: ["boss", "player"],
    params_schema: { type: "object", required: ["volume"] },
  },
  () => ({ kind: "succeeded", effect_summary: { ok: true } })
);
const failRep = await npc.handleOriginExecute({
  slot_id: "slot-ts-npc",
  epoch: 1,
  kind: "invoke",
  execution_id: "ex-ts-1",
  method_id: "#07",
  actor: "boss",
  args: {},
});
assert.equal(failRep.state, "failed");
assert.equal(failRep.failure_reason, "missing_required_arg:volume");

console.log(`TypeScript @g2a/interaction-origin SelfTest PASSED | JCS=${got}`);
