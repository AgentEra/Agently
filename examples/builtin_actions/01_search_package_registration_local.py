import os
from pprint import pprint

from agently import Agently
from agently.builtins.actions import Search


def build_agent():
    agent = Agently.create_agent()
    search = Search(
        proxy=os.getenv("SEARCH_PROXY") or None,
        timeout=10,
        backend=os.getenv("SEARCH_BACKEND", "duckduckgo"),
        region=os.getenv("SEARCH_REGION", "us-en"),
    )
    agent.use_actions(search)
    return agent


def main():
    agent = build_agent()
    agent_tag = f"agent-{ agent.name }"
    specs = agent.action.get_action_list(tags=[agent_tag])
    print("[REGISTERED_SEARCH_ACTIONS]")
    pprint(
        [
            {
                "action_id": spec.get("action_id"),
                "executor_type": spec.get("executor_type"),
                "component": spec.get("meta", {}).get("component"),
            }
            for spec in specs
            if str(spec.get("action_id", "")).startswith("search")
        ]
    )

    assert {"search", "search_news", "search_wikipedia", "search_arxiv"}.issubset(
        {str(spec.get("action_id")) for spec in specs}
    )

    if os.getenv("RUN_REAL_SEARCH") != "1":
        print("[SKIP_REAL_SEARCH] Set RUN_REAL_SEARCH=1 to call the configured search backend.")
        return

    result = agent.action.execute_action(
        "search",
        {
            "query": os.getenv("SEARCH_QUERY", "Agently Action Runtime"),
            "max_results": 3,
        },
    )
    print("[ACTION_RESULT]")
    pprint(result)


if __name__ == "__main__":
    main()

# Expected key output:
# [REGISTERED_SEARCH_ACTIONS] lists search, search_news, search_wikipedia, and search_arxiv.
# By default the script prints [SKIP_REAL_SEARCH] and does not call the network.
# With RUN_REAL_SEARCH=1, [ACTION_RESULT] contains a real search ActionResult.
