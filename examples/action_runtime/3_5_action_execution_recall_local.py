from pathlib import Path
from pprint import pprint

from agently import Agently
from agently.core import Action


agent = Agently.create_agent()
workspace = Path(__file__).resolve().parent

agent.enable_shell(
    root=workspace,
    commands=["pwd"],
    action_id="inspect_workspace",
)

records = agent.action.execute_action(
    "inspect_workspace",
    {"cmd": "pwd", "workdir": str(workspace)},
    purpose="Inspect workspace path",
)

print("RAW ACTION RECORD")
pprint(records)

print("\nMODEL-VISIBLE ACTION RESULTS")
pprint(Action.to_action_results([records]))

artifact_ref = records["artifact_refs"][0]
raw_artifact = agent.action.read_action_artifact(
    artifact_id=artifact_ref["artifact_id"],
    action_call_id=artifact_ref["action_call_id"],
)

print("\nRECALLED RAW ARTIFACT")
pprint(raw_artifact)

# Expected key output:
# RAW ACTION RECORD contains model_digest and artifact_refs.
# MODEL-VISIBLE ACTION RESULTS contains the compact digest, not only raw stdout.
# RECALLED RAW ARTIFACT returns the saved action input for inspect_workspace.
