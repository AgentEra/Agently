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
| Agently 4.2 | The higher Python minimum takes effect on the 4.2 line. | Keep using the 4.1 line if you still need Python 3.10. | Deferred to 4.2. | Package compatibility manifest. |

To hide the notice for a script, set the environment variable before Agently is imported:

```bash
AGENTLY_PYTHON_310_NOTICE=0 python your_script.py
```

On PowerShell, set `$env:AGENTLY_PYTHON_310_NOTICE = "0"` before running the script.
The setting only controls this notice.

Thanks to Dex Hünter (@dexhunter) for the [StateData optimization](https://github.com/AgentEra/Agently/pull/370)
and the [sanitizer optimization](https://github.com/AgentEra/Agently/pull/371).
