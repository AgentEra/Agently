# Agently Utils Guide (Agent-Readable)

Use this as a compact, agent-oriented guide to the utilities in `agently/utils`. It is intentionally brief and practical.

## Quick Map (TL;DR)
- data shaping: `DataFormatter`, `StateData`, `SerializableStateData`, `Settings`
- path and JSON helpers: `DataLocator`, `DataPathBuilder`, `StreamingJSONCompleter`, `StreamingJSONParser`
- async/sync bridging: `FunctionShifter`, `GeneratorConsumer`
- deprecations: `DeprecationWarnings`
- dynamic deps: `LazyImport`
- storage: `Storage`, `AsyncStorage`
- misc: `Logger`, `Messenger`, `PythonSandbox`
- legacy: `old_RuntimeData` (avoid unless you must keep backward behavior)

## Utilities

### DataFormatter
Purpose: normalize complex values into safe, serializable, or string forms, and do placeholder substitution.

Key methods:
- `sanitize(value, remain_type=False)`: convert complex objects into JSON-ish values. Handles `datetime`, `StateData`, `pydantic.BaseModel`, and typing constructs like `list[T]`, `Union`, `Literal`.
- `to_str_key_dict(value, value_format=None, default_key=None, default_value=None)`: ensure dict keys are strings and values optionally sanitized or stringified. If input is not a dict, can wrap with `default_key`.
- `from_schema_to_kwargs_format(schema)`: convert JSON Schema object fields into Agently kwargs-style `(type, desc)` mapping.
- `substitute_placeholder(obj, variable_mappings, placeholder_pattern=None)`: recursive replace `${key}` placeholders in strings, dicts, lists, sets, tuples.

When to use:
- Before logging/serialization.
- When building structured prompts from schemas.
- When injecting env variables into settings or prompts.

### DataLocator
Purpose: locate values by path, and extract JSON blocks from mixed text.

Key methods:
- `locate_path_in_dict(dict, path, style="dot"|"slash", default=None)`: safe deep lookup with `a.b[0]` or `/a/b/0` styles.
- `locate_all_json(text)`: scan text and return all JSON-like blocks.
- `locate_output_json(text, output_prompt_dict)`: pick the most likely JSON block matching your output schema.

When to use:
- Parsing LLM responses that mix text + JSON.
- Robust extraction for streaming parsers.

### DataPathBuilder
Purpose: convert and reason about dot/slash paths, and extract expected parsing paths from a schema-like dict.

Key methods:
- `build_dot_path(keys)`, `build_slash_path(keys)`
- `convert_dot_to_slash(dot_path)`, `convert_slash_to_dot(slash_path)`
- `extract_possible_paths(schema, style="dot")`: find all possible paths.
- `extract_parsing_key_orders(schema, style="dot")`: paths in definition order (used by streaming parser).
- `get_value_by_path(data, path, style="dot")`: retrieve values, supports `[*]` wildcard expansion.

When to use:
- Streaming JSON parsing with ordered fields.
- Mapping UI updates to schema paths.

### FunctionShifter
Purpose: deprecated compatibility facade over Agently-Stage adapters.

Key methods:
- `syncify(func)`: delegates to `Stage.as_sync()`.
- `asyncify(func)`: delegates to `Stage.as_async()`.
- `future(func)`: returns a loop-neutral `StageHandle`.
- `syncify_async_generator(async_gen)`: delegates to `StageCallBridge.iter_sync()`.
- `asyncify_sync_generator(sync_gen)`: delegates to `StageCallBridge.iter_async()`.
- `auto_options_func(func)`: delegates to the pure `filter_callable_options()` helper.

Each `syncify()` / `asyncify()` call owns one automatic Stage scope and waits
for Stage-owned settlement. Advanced stream conversion, injected Stage or
executor ownership, and explicitly lightweight adaptation remain
`StageCallBridge` responsibilities. Runtime schedulers that own a broader
lifecycle may continue to use `StageCallBridge(..., managed=True)` directly.

When to use:
- Only for compatibility with existing integrations.
- New scalar adapters should use `Stage.as_sync()` / `Stage.as_async()`.
- Runtime code that requires lightweight or advanced bridge behavior should
  import `default_stage_call_bridge` or `StageCallBridge` from `agently_stage`.
- Signature-only option filtering should use `filter_callable_options` from
  `agently.utils`.

### GeneratorConsumer
Purpose: fan out a generator or async generator to multiple consumers, replay history, and handle errors.

Usage:
- Wrap a generator, then call `get_async_generator()` for multiple async consumers or `get_generator()` for sync.
- `get_result()` waits for completion and returns full history.
- `close()` cancels and notifies listeners.

When to use:
- Broadcast streaming output to multiple subscribers.
- Merge history + live updates reliably.

### DeprecationWarnings
Purpose: emit Agently deprecation warnings once per deprecated API per Python process.

Key methods:
- `warn_deprecated_once(key, message, stacklevel=2)`: emit a `DeprecationWarning` for a stable API key only on first use.
- `log_deprecated_once(key, logger, message)`: log a deprecation message once for a stable API key.
- `reset_registry()`: clear the once-per-process registry for tests.

When to use:
- Any framework deprecation warning.
- Do not use this as a generic warning helper.

### LazyImport
Purpose: import optional deps, report structured missing-dependency facts, and
optionally auto-install via pip only when explicitly enabled.

Key methods:
- `from_import(from_package, target_modules, auto_install=False, version_constraint=None, install_name=None)`
- `import_package(package_name, auto_install=False, version_constraint=None, install_name=None)`

Notes:
- By default this does not prompt or mutate the environment. Missing packages
  and version mismatches raise `LazyImportDependencyError`, an `ImportError`
  subclass with `payload`, `install_command`, and `install_command_text`.
- `auto_install=True` is reserved for explicit interactive CLI/dev paths. It
  must be declared at the `LazyImport` call site, prompts on the command line,
  and runs the suggested pip install command only after the user confirms.

### Logger
Purpose: create a consistent logger with optional uvicorn integration.

Key pieces:
- `create_logger(app_name="Agently", log_level="INFO")` returns `AgentlyLogger` with `raise_error` helper.

### RuntimeEmitter
Purpose: compatibility wrapper for Event Center RuntimeEvent emission.

Key method:
- `create_runtime_emitter(source)` delegates to `agently.base.event_center`.

### PythonSandbox
Purpose: compatibility utility for evaluating small expressions with restricted builtins and whitelisted return types.

This is not an OS/process isolation boundary and is not the owner for unified
code execution. Use ActionRuntime code execution with an isolation-capable
ExecutionResource provider for untrusted Python, Node.js, Go, or C++ code.

Key behaviors:
- `run(code)` executes in a sandbox; raises if a return type is not in `allowed_return_types`.
- `preset_objects` are wrapped to block private attributes and enforce safe return types.

When to use:
- Run short user-defined expressions or filters with safety checks.

### StateData / StateDataNamespace
Purpose: runtime-scoped hierarchical data with inheritance, dot-path access, and merge-friendly set semantics.

Key behaviors:
- `get(key, default=None, inherit=True)`: inherited view by default.
- `set` and `__setitem__` merge dict/list/set values rather than replace (unless you use `cover=True` internally).
- dot-path access: `data["a.b.c"]`.
- `namespace("path")` returns a namespace view.
- `dump("json"|"yaml"|"toml")`, `load(...)` support.

When to use:
- Store workflow state, memo, runtime configs.

### SerializableStateData / SerializableStateDataNamespace
Purpose: same API as `StateData` but value types restricted to JSON-serializable shapes.

When to use:
- Settings and serialized runtime state.

### Settings / SettingsNamespace
Purpose: settings with mapping shortcuts and env substitution.

Key behaviors:
- `register_path_mappings("short", "actual.path")`: alias keys.
- `register_kv_mappings("key", "value", actual_settings)`: map a key+value to a settings dict.
- `load(..., auto_load_env=False, raise_empty=False)`: settings-level load API for JSON/YAML/TOML text or files with optional `${ENV.X}` expansion.
- `set_settings(key, value, auto_load_env=False, raise_empty=False)`: apply mappings and optionally expand `${ENV.X}`.

When to use:
- Global or per-agent configuration with shortcuts.

### Storage / AsyncStorage
Purpose: simple SQLModel-based persistence with sync/async APIs.

Key behaviors:
- requires `sqlmodel`, `sqlalchemy`, `aiosqlite` via `LazyImport`.
- `set(obj|list)` merges into DB.
- `get(model, where=..., first=False, limit=..., offset=..., order_by=...)`.
- `create_tables()` calls `SQLModel.metadata.create_all`.

When to use:
- local state persistence for agents or tools.

### StreamingJSONCompleter
Purpose: complete partial JSON strings by closing open strings, comments, or brackets.

Key method:
- `append(data)` then `complete()` to get best-effort JSON.

When to use:
- streaming LLM output where JSON is partial.

### StreamingJSONParser
Purpose: parse streaming JSON and emit incremental updates (`delta`) and completion events (`done`).

Key behaviors:
- Uses `DataLocator` + `StreamingJSONCompleter` to find and parse JSON in noisy streams.
- Tracks schema order via `DataPathBuilder.extract_parsing_key_orders`.
- `parse_chunk(chunk)` yields `StreamingData` events.
- `parse_stream(chunk_stream)` yields events and finalizes at end.

When to use:
- UI streaming updates for structured output.
