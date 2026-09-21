# Jev judgments in output schemas

Development feature: `Probability`, `Choice`, and `Score` declare atomic judgment
fields. Agent Execution combines Jev evaluation with ordinary LLM output and
assembles the result. Even `direct` passes through Execution; a single Agent
execution can contain several ModelRequests.

```python
import os
from dotenv import find_dotenv, load_dotenv
from agently import Agently, Probability, Choice, Score

load_dotenv(find_dotenv(usecwd=True))
Agently.set_settings("Jev", {"api_key": os.environ["JEV_API_KEY"]})
Agently.set_settings("system_one", {"provider": "Jev"})
agent = Agently.create_agent()
execution = agent.input("All my payouts failed. Please fix this today.").output({
    "urgent": Probability("Does the customer request a same-day fix?"),
    "team": Choice("Which team fits this request?", {
        "billing": "Payments and payouts", "sales": "Purchases",
    }),
    "urgency": Score("How urgent is this request?", [
        "No urgency", "Time-sensitive", "Explicit same-day request",
    ]),
})
result = execution.get_data(max_retries=1)
details = execution.get_meta()["judgment"]
```

| Declaration | Business value |
|---|---|
| `Probability(question)` | Float between 0 and 1 |
| `Choice(question, options)` | One option key; up to 255 candidates |
| `Score(question, options)` | Float between 0 and `len(options)-1`; 2–10 ordered grades |

Score uses an ordered scale and can fall between grades. Use Choice for
unordered categories such as trusted, untrusted, and unknown. Questions must
state the complete judgment; field names do not supply missing instructions.
Jev evaluates questions; it does not generate explanations or chain-of-thought.

## Independent configuration

`Jev` maps to `plugins.ModelRequester.Jev`. Request-local settings override
Agent settings, which override global settings. The default API base is
`https://api.typesafe.ai/v1`, model `jev-latest`, timeout 60 seconds, and batch
size 64. Set `base_url`, `model`, `timeout`, or `batch_size` under `Jev` as needed.
Credentials stay separate from `OpenAICompatible` and are never prompt context.

SystemOne is disabled without a `system_one` model configuration. To select Jev,
set `Agently.set_settings("system_one", {"provider": "Jev"})` as well as Jev credentials.
Use `.use_system_one(False)` to return templates to the ordinary LLM. Explicit
`Agently.set_settings("Jev.enabled", False)` also selects the LLM, even with
stale Jev credentials. Explicit enablement or a partial connection configuration
without a valid API key fails before dispatch. A failed Jev request never
silently falls back. LLM estimates are identified as such and have no fabricated
native probability distribution or confidence.

Pure static Jev output needs no LLM connection. Mixed output needs a configured
ordinary ModelRequester as well. Keep the LLM active; Execution selects Jev
locally for its judgment stages. Without output dependencies, disabled Jev
uses one ordinary structured LLM request.

## Output dependencies

```python
execution = agent.input(source_records).output({
    "items": [{
        "name": (str, "Name from the supplied record"),
        "message": (str, "The same record's message, copied verbatim"),
        "urgent": Probability(
            "Does this message request a same-day response?",
            from_output="items[].message",
        ),
    }],
})
result = execution.get_data()
```

Here the LLM first produces and validates the items and messages; unneeded ordinary fields are generated after the judgments. Jev then
evaluates each matching message; Host inserts the judgments into the fixed
items. Empty lists cause no per-item evaluations. Root lists use `[].name`.
`[]` and `[*]` mean the same wildcard. Nested lists are supported. A judgment
outside a list reads the matched collection; a judgment inside the same list
binds to its current item. Different lists are never implicitly zipped.
Explicit numeric indices select a particular item.

Static ready judgments run before ordinary fields and become read-only evidence
for the LLM. Judgments may also reference earlier judgment fields, producing
additional dependency stages. Unknown paths, self references, cycles, and
ambiguous cross-list wildcard bindings fail before network calls. Dependencies
are explicit; prose descriptions are not inspected to infer execution order.

`from_output` also accepts a non-empty list of unique paths. The bound value is
an object keyed by the supplied paths, even for a one-element list. Each path
uses the same per-item/collection rules as a single path.

`after_output` accepts a path or path list. It requires those fields to finish
before evaluation, but does not add their values to the evaluator's bound input.
Both sets of ordinary fields can be generated in **one LLM request**, preserving
the original schema order. It does not require a separate LLM request per field.
Original input/info/instruct remain shared context; this is not data redaction.

```python
{
    "evidence": (str, "Cite source facts used to form the conclusion"),
    "conclusion": (str, "A concise conclusion grounded in evidence"),
    "risk": Score(
        "How serious is the issue described in the conclusion?",
        ["Minor", "Material", "Critical"],
        from_output=["conclusion"],
        after_output=["evidence"],
    ),
    "summary": (str, "Explain the conclusion and accepted risk score"),
}
```

This produces evidence and conclusion together, evaluates the conclusion, then
generates the summary. The evidence output value is not added to the Jev request.

## Extensible templates and execution

`OutputTemplate` is a provider-independent base with `question`, `from_output`,
`after_output`, and `to_schema()`. Probability, Choice and Score are sibling
templates. They do not encode Jev protocol names. Jev translates the templates
it supports; other templates use the ordinary LLM requester.

A custom template subclasses `OutputTemplate` and returns a schema tuple:
`(PythonAnnotation, description, True, {"judgment": True})`. Its annotation can
also be a Pydantic model for structured answers. See
[output_templates.py](../../../examples/output_templates.py).

A template describes the result contract; execution strategy selects the model
and its reasoning settings. A small LLM or a model's supported no-reasoning
option can produce these templates and other structures. Configure those options
through the existing model/provider settings. Use the [SystemOne model-selection layer](system-one.md) to configure a separate
model for templates. No template/model combination promises speed or Jev's
native probability semantics.

Current composition accepts JSON output, dictionaries and single-item list
schemas. Literal keys containing `.`, brackets, or `*` are rejected. Optional
judgment unions and composition with `auto_continue` are not supported. There
is no new dependency syntax for ordinary LLM fields. `${OUTPUT...}` remains a
prompt label reference, not a runtime value locator.

## Results and failure handling

`get_data()` returns the assembled business value; `get_data_object()` returns
its validated model. `get_meta()["judgment"]` retains field sources, request
ids, revisions, stage status/timing and native Jev answers/model/usage. Native
Choice and Score distributions, confidence and legends remain available there.
The LLM cannot overwrite Jev fields. Final validation, artifact and review
policies run against the assembled output.

All composition stages share `max_retries`. A stage's first call is normal
work; each additional attempt consumes one retry. Successful preceding stages
are retained. Configuration/dependency errors and cancellation do not retry.
Stages disable nested provider retries, key failover and request-local output
repair. Stage progress is provisional; wait for final output before acting.

For a standalone atomic Jev request, explicitly select
`request.set_settings("plugins.ModelRequester.activate", "Jev")` and supply
only static judgment fields. Mixed/dynamic output and `from_output` need Agent
Execution. Standalone native details are in `get_data(type="original")`.

See the [runnable example](../../../examples/jev_output.py) and
[TypeSafe API reference](https://docs.typesafe.ai/api).
