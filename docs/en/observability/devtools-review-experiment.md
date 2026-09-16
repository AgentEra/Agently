# DevTools 0.2 review loop

This feature requires `agently-devtools >=0.2.0,<0.3.0`; 0.1.x does not
provide `/reviews`. The Agently RuntimeEvent protocol is
unchanged and DevTools remains optional.

Inspect actual inputs, prompts, outputs and handoffs in run details, then attach
a review to a run or one of its events. Record a stage criterion and distinguish
observations, hypotheses and suggestions. An Agently-Skills-guided Coding Agent
reads the same record and original evidence, changes only authorized files,
reruns comparable cases and appends its findings with the new run references.
The designated reviewer inspects those results before accepting or reopening.

`addressed` means ready for review. `accepted` is only a review-record status;
it does not change a runtime result or authorize code execution. Author names
are local attribution, not authenticated identities. Missing original evidence
is reported explicitly and cannot be replaced by comment text.

```python
import httpx
with httpx.Client(base_url=base_url, headers=headers) as client:
    page = client.get('/reviews', params={
        'app_id': app_id, 'group_id': group_id, 'status': 'open',
        'limit': 50, 'offset': 0,
    }).raise_for_status().json()['data']
    # Follow next_offset; /reviews/{review_id} returns full reply history.
```

Writes use a client operation_id for idempotency and revision for optimistic
concurrency. Compare the same or explicitly versioned cases and quality criteria;
record source revisions, model configuration, calls, usage, timing and limits.
UI and API share the same persisted records and never automatically execute a
change, retry a workflow or merge a branch.

DevTools 0.2 displays missing provider token counts as `unavailable`. Character estimates remain separate diagnostics, not token or cost measurements.

## Complete path (schema 3)

Find a review by app/group/status in **Review inbox**, then inspect **Current
evidence**, **Changes & decisions**, **Experiments**, and **Reply & history** for the same
object. Proposals carry source versions, node responsibilities, handoffs and exact
current/proposed slot content. Declare fixed cases, versioned variants and node
versus end-to-end criteria before linking an existing EvaluationRunner suite.
Missing results do not mean acceptance.

Copy **Coding Agent handoff** into the authorized task. Guided by Agently-Skills,
the Agent verifies original evidence and source files, applies an authorized
revision, and returns `application` with `plan_revision`, applied source reference,
actual request event IDs and terminal rerun IDs. Source references are author
claims, not automatic source verification. Reviewers compare proposal, applied
source, dispatched requests and effects. A revised plan reopens review; stale
application references are rejected.

Drafts survive navigation and refresh within the browser tab. After a conflict,
inspect current history and explicitly adopt its revision. Diagnostics are
collapsible, reconnect refreshes reviews, and reference errors appear at the
form. This does not execute code or grant authority.

## Workspace and explicit decisions (schema 3)

The sidebar separates Runs, Evaluations, Review & revise, Playground, Logs and
support. URL state retains module/object and list scope across navigation.
Interactive remains a separate application surface with inline schema/JSON input.

Plan changes identify flow, Prompt or expected behavior, with before/after text,
evidence, risk, verification and selected/proposed/deferred decisions. Work modes
separate investigation, bounded comparison and implementation. Only selected
changes enter implementation handoff; dependencies must be selected together.
Save decisions before previewing handoff. Historical experiments do not renew a
spent call budget. Application evidence binds the current plan_revision and exact
change_ids alongside real request events and reruns. Old proposals are not adopted
automatically. Source declarations and semantic equivalence still require review.
