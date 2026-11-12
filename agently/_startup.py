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

import os
import sys


if (
    sys.version_info[:2] == (3, 10)
    and os.environ.get("AGENTLY_PYTHON_310_NOTICE") != "0"
    and sys.stderr is not None
):
    try:
        print(
            "Agently: Python 3.10 has reached its official end of life. "
            "Agently 4.2 and later will require Python 3.11 or newer. "
            "Set AGENTLY_PYTHON_310_NOTICE=0 before importing agently to hide this notice.",
            file=sys.stderr,
        )
    except (OSError, ValueError):
        # A closed or unavailable stderr must not prevent startup.
        pass
