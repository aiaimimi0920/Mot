# g2a-interaction-origin (`0.0.1`)

Zero-dependency Python 3.11+ **Interaction Origin SDK** implementing Tier 1 of the **G2A `V0.0.1`** protocol specification, including all five canonical genre reference hosts (`spatial`, `strategy_card`, `workshop`, `deduction`, `tactical_arena`).

## Installation

```sh
pip install g2a_interaction_origin-0.0.1-py3-none-any.whl
```

## Quick Start

```python
from g2a_origin import OriginSDK, CapabilityEntry, ImmediateSucceeded

sdk = OriginSDK.from_local_hub("slot-companion")
sdk.start_loopback_execute_listener()
sdk.bind_actor_subject("companion", {})
sdk.register_capability(
    CapabilityEntry("#01", "inspect", "Inspect nearby object", ["companion"], {"type": "object"}),
    effect_handler=lambda actor, subj, args, tok: ImmediateSucceeded({"inspected": True}),
)
sdk.publish_capabilities_and_baseline()
```
