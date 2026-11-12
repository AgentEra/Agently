# Agently 4.1.4.11 release notes

Agently 4.1.4.11 still supports Python 3.10. Python 3.10 reached the end
of [official maintenance](https://devguide.python.org/versions/#unsupported-versions)
on October 1, 2026. Agently 4.2 and later require Python 3.11 or newer.
Agently 4.2 is still in development.

| Area | Change | Recommended usage | Compatibility | Verification |
|---|---|---|---|---|
| Startup | On Python 3.10, Agently prints one notice to stderr on first import per process. | Upgrade to Python 3.11 or newer, or set `AGENTLY_PYTHON_310_NOTICE=0` before import. | Python 3.10 remains supported in 4.1.4.11. | Fresh-process startup and suppression tests. |
| State copying | Skip redundant copies of exact built-in immutable values. | Existing calls benefit automatically. | Copy behavior for mutable values and subclasses is preserved. | StateData and Agent execution regression tests. |
| Data sanitization | Speed up traversal of ordinary nested containers. | Existing calls benefit automatically. | Key conversion, type options, and read-only mapping support are preserved. | DataFormatter, context projection, and TaskBoard evidence tests. |
| Native tool calls | Forward Action definitions to Chat Completions and read tool calls in both response modes. | Use `planning_protocol="native_tool_calls"` with a model that supports tools. | Existing explicit function definitions take precedence by name. | Real model and requester regression checks. |
| Recovery | Restore TaskBoard ticks and task iterations independently. | Use the existing resume methods with record-store recovery enabled. | Older snapshots are readable. | TaskBoard checkpoint and result resume tests. |
| Retrieval | Opt in to a dedicated reranker with `model_type="rerank"` and `RerankProvider`. | Bind a rerank provider to RecordStore. | The default retrieval and reranking behavior is preserved. | Real reranker and RecordStore tests. |
| Provider docs and tests | Clarify the MiniMax Messages URL and extend Silero stream and cancellation tests. | Use the existing MiniMax settings. | Silero runtime behavior is preserved. | Provider and audio regression tests. |
| Agently 4.2 | The higher Python minimum takes effect on the 4.2 line. | Keep using the 4.1 line if you still need Python 3.10. | Deferred to 4.2. | Package compatibility manifest. |

To hide the notice for a script, set the environment variable before Agently is imported:

```bash
AGENTLY_PYTHON_310_NOTICE=0 python your_script.py
```

On PowerShell, set `$env:AGENTLY_PYTHON_310_NOTICE = "0"` before running the script.
The setting only controls this notice.

Thanks to Dex Hünter (@dexhunter) for the [StateData optimization](https://github.com/AgentEra/Agently/pull/370)
and the [sanitizer optimization](https://github.com/AgentEra/Agently/pull/371).
