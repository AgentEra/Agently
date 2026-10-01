# SystemOne: a dedicated model for output templates

SystemOne is the model-selection layer inside Agent Execution for fast judgments.
It is separate from output-template contracts and the ordinary LLM configuration.
Jev, a small LLM, or a supported non-reasoning model can fill this role.

```python
import os
from dotenv import find_dotenv, load_dotenv
from agently import Agently, Probability

load_dotenv(find_dotenv(usecwd=True))
agent = Agently.create_agent()
agent.set_settings("OpenAICompatible", {
    "base_url": os.environ["OMLX_BASE_URL"],
    "api_key": os.environ["OMLX_API_KEY"],
    "model": os.environ["LLM_MODEL"],
})
agent.set_settings("system_one", {
    "provider": "OpenAICompatible",
    "base_url": os.environ["OMLX_BASE_URL"],
    "api_key": os.environ["OMLX_API_KEY"],
    "model": "Qwen3.5-9B-MLX-4bit",  # Verify the API ID in /v1/models.
    "request_options": {
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": 800,
    },
})
result = agent.use_system_one(True).input("Please fix the payout today.").output({
    "urgent": Probability("Does the customer explicitly request a same-day fix?"),
    "summary": (str, "Summarize the supplied facts and accepted urgency judgment."),
}).get_data(max_retries=0)
```

Configuring `system_one` enables it by default. With no configuration or an empty
object it is off. `enabled=False` or `.use_system_one(False)` overrides the default;
`.use_system_one(True)` requires a usable model configuration. The Agent method
creates an execution, while the Execution method changes that run's draft.
It does not mutate the Agent's defaults. Configure before starting. A started/completed execution rejects reconfiguration;
create a fresh execution for another run, preserving the old result. Wrong/partial configuration
fails before any model is dispatched; runtime failure does not silently switch models.
An output without templates adds no SystemOne request.

Instead of an inline profile, use `{"model_key": "fast"}` to select an existing
`model_pool` entry and its `model_profiles`/API key configuration. Do not combine
a model key with inline provider fields. Inline profiles use the existing model
profile field names, including `request_options` and `client_options`.
`SystemOneSettings` is available from `agently.types.settings` for typed setup.
Request-local settings override Agent settings and global settings.

A SystemOne child uses its provider defaults and explicit profile, not the ordinary
model's auth, headers, model, request options or prompt options. Ordinary input,
info and instruct remain the shared task context. For Jev, keep credentials in
`Jev` and select `system_one={"provider": "Jev"}`. The `Jev.enabled=False` switch
also disables that selected producer. Jev credentials alone do not enable SystemOne.

## Execution and evidence

- Independent template fields: SystemOne, then ordinary fields consume its results.
- `from_output` / `after_output`: ordinary prerequisites may share one ordered
  request, followed by SystemOne, then remaining ordinary fields.
- Disabled: templates use the ordinary LLM; explicit dependencies remain, and
  independent output can be combined into one request.
- Custom `OutputTemplate` results may be strings or structured values. Jev only
  handles its supported templates; other templates use the ordinary LLM when
  Jev is selected. A SystemOne LLM handles all compatible templates itself.

The same Execution/TriggerFlow owns retries, cancellation and Host assembly.
The template evidence passed onward includes descriptions, score rubrics, choice
meanings and JSON Schema. A numeric score alone does not explain its scale.
Metadata at `execution.get_meta()["judgment"]` records SystemOne activation and
each stage's provider, model, SystemOne role, latency and observed reasoning
character count. No secrets are included in that projection.

A provider-specific `enable_thinking=False` is not a universal Agently reasoning
flag. Verify support in your deployed provider and model. LLM probabilities and
scores remain generated estimates; they do not acquire Jev's calibration or native
distributions. Extra request boundaries and model loading may offset inference
savings. See [system_one.py](../../../examples/system_one.py) for on/off, dynamic,
and custom-template runs; the limited local comparison did not establish a total
latency improvement.
