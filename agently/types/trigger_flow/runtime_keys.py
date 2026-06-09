# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


TRIGGER_FLOW_CHECKPOINT_SCHEMA_VERSION = 1
TRIGGER_FLOW_CHECKPOINT_KIND = "triggerflow.execution_snapshot"

PARENT_SIGNAL_ID_META_KEY = "_triggerflow_parent_signal_id"
AGGREGATION_SCOPE_META_KEY = "_triggerflow_aggregation_scope"
SELF_RESUME_COUNT_META_KEY = "_triggerflow_self_resume_count"
SELF_RESUME_MAX_META_KEY = "_triggerflow_self_resume_max"

DURABLE_SYSTEM_STATE_KEYS = (
    "when_states",
    "batch_states",
    "collect_states",
    "for_each_results",
    "match_results",
)

TRANSIENT_AGGREGATION_STATE_KEYS = (
    *DURABLE_SYSTEM_STATE_KEYS,
    "batch_semaphores",
    "batch_fanout_semaphores",
    "for_each_semaphores",
)
