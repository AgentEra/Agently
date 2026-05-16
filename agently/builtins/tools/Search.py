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

from agently.builtins.actions.Search import Search as _Search


class Search(_Search):
    """Legacy import facade for `agently.builtins.actions.Search`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tool_info_list = [
            {
                "name": "search",
                "desc": "Search the web with {query}",
                "kwargs": {"query": [("str", "search query")]},
                "func": self.search,
            },
            {
                "name": "search_news",
                "desc": "Search news with {query}",
                "kwargs": {"query": [("str", "search query")]},
                "func": self.search_news,
            },
            {
                "name": "search_wikipedia",
                "desc": "Search wikipedia with {query}",
                "kwargs": {"query": [("str", "search query")]},
                "func": self.search_wikipedia,
            },
            {
                "name": "search_arxiv",
                "desc": "Search arXiv with {query}",
                "kwargs": {"query": [("str", "search query")]},
                "func": self.search_arxiv,
            },
        ]
