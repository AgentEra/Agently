# Built-in Action Package Examples

These examples use the Action-native import path:

```python
from agently.builtins.actions import Browse, Search
```

Use this directory for new code. Historical built-in tool examples were moved to
`examples/archived/builtin_tools/`.

Every current example should be directly runnable in its declared environment
and include an `Expected key output` comment showing the important output shape.

## Files

| File | Requires model? | External dependency | What it shows |
|---|---:|---|---|
| `01_search_package_registration_local.py` | No | None unless `RUN_REAL_SEARCH=1` | Mount `Search(...)` with `agent.use_actions(search)` and inspect registered actions. |
| `02_browse_bs4_local_http.py` | No | `beautifulsoup4` | Browse a local HTTP page with the BS4 fallback path. |
| `03_search_browse_agent_ollama.py` | Yes | Ollama, optional search/browse deps | Let a model use Search and Browse packages together. |

## Run

```bash
python examples/builtin_actions/01_search_package_registration_local.py
python examples/builtin_actions/02_browse_bs4_local_http.py
```

For the model-driven example, start Ollama and set optional search proxy values
if needed:

```bash
ollama pull qwen2.5:7b
python examples/builtin_actions/03_search_browse_agent_ollama.py
```
