"""Compile explicit output dependencies; no semantic inference or model calls."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter

from agently.types.data.judgment import OutputTemplate
from agently.utils import DataLocator

Path = tuple[str | int, ...]
_OMIT = object()


def has_judgment(value: Any) -> bool:
    if isinstance(value, OutputTemplate):
        return True
    if isinstance(value, Mapping):
        return any(has_judgment(child) for child in value.values())
    if isinstance(value, (tuple, list)):
        return any(has_judgment(child) for child in value)
    return False


def path_text(path: Path) -> str:
    text = ""
    for part in path:
        if part == "*":
            text += "[]"
        elif isinstance(part, int):
            text += f"[{part}]"
        else:
            text += ("." if text else "") + part
    return text


def parse_path(text: str) -> Path:
    parts: list[str | int] = []
    for section in text.split("."):
        match = re.fullmatch(r"([^\[\].]+)?(?:\[(\d*|\*)\])?", section)
        if match is None or not section:
            raise ValueError(f"Unsupported from_output path: {text!r}.")
        key, index = match.groups()
        if key:
            parts.append(key)
        if index is not None:
            parts.append("*" if index in ("", "*") else int(index))
    if not parts:
        raise ValueError("from_output requires a path.")
    return tuple(parts)


def _unwrap(value: Any) -> Any:
    return value[0] if isinstance(value, tuple) and value else value


def project_ordinary(schema: Any) -> Any:
    if isinstance(schema, OutputTemplate):
        return _OMIT
    if isinstance(schema, tuple) and schema:
        child = project_ordinary(schema[0])
        return _OMIT if child is _OMIT else (child, *schema[1:])
    if isinstance(schema, Mapping):
        result = {key: project_ordinary(value) for key, value in schema.items()}
        result = {key: value for key, value in result.items() if value is not _OMIT}
        return result if result or not schema else _OMIT
    if isinstance(schema, list):
        if len(schema) != 1:
            raise ValueError("Judgment output arrays require exactly one item schema.")
        child = project_ordinary(schema[0])
        return [{} if child is _OMIT else child]
    return schema


@dataclass(frozen=True)
class JudgmentField:
    path: Path
    declaration: OutputTemplate
    sources: tuple[Path, ...]
    prerequisites: tuple[Path, ...]


class JudgmentSchema:
    def __init__(self, schema: Any):
        self.schema = schema
        self.fields: dict[Path, JudgmentField] = {}
        self.nodes: dict[Path, Any] = {}
        self.ordinary = project_ordinary(schema)
        self.remaining: dict[Path, Any] = {}

        def visit(value: Any, path: Path) -> None:
            self.nodes[path] = value
            raw = _unwrap(value)
            if isinstance(raw, OutputTemplate):

                def paths(value: Any) -> tuple[Path, ...]:
                    values = [value] if isinstance(value, str) else value or ()
                    result = tuple(parse_path(source) for source in values)
                    if len(set(result)) != len(result):
                        raise ValueError("Output dependency paths must be unique after normalization.")
                    return result

                self.fields[path] = JudgmentField(path, raw, paths(raw.from_output), paths(raw.after_output))
            elif isinstance(raw, Mapping):
                for key, child in raw.items():
                    if not isinstance(key, str) or not key or any(char in key for char in ".[]*"):
                        raise ValueError(
                            f"Unsupported output key for judgment binding at {path_text(path)!r}: {key!r}."
                        )
                    visit(child, (*path, key))
            elif isinstance(raw, list):
                if len(raw) != 1:
                    raise ValueError("Judgment output arrays require exactly one item schema.")
                visit(raw[0], (*path, "*"))

        visit(schema, ())
        self.dependencies: dict[Path, set[Path]] = {}
        self.needs_ordinary: set[Path] = set()
        for path, item in self.fields.items():
            dependencies: set[Path] = set()
            if "*" in path:
                self.needs_ordinary.add(path)
            for source in (*item.sources, *item.prerequisites):
                canonical = tuple("*" if isinstance(part, int) else part for part in source)
                if canonical not in self.nodes:
                    raise ValueError(f"{path_text(path)}: unknown output dependency {path_text(source)!r}.")
                target_lists = {path[: i + 1] for i, part in enumerate(path) if part == "*"}
                source_lists = {canonical[: i + 1] for i, part in enumerate(source) if part == "*"}
                if target_lists and source_lists - target_lists:
                    raise ValueError(f"{path_text(path)}: ambiguous cross-list from_output binding.")
                source_dependencies = {other for other in self.fields if other[: len(canonical)] == canonical}
                dependencies.update(source_dependencies)
                referenced = [value for other, value in self.nodes.items() if other[: len(canonical)] == canonical]
                if not source_dependencies or any(
                    not isinstance(_unwrap(value), (OutputTemplate, Mapping, list)) for value in referenced
                ):
                    self.needs_ordinary.add(path)
            self.dependencies[path] = dependencies
        required = {
            tuple("*" if isinstance(part, int) else part for part in source)
            for item in self.fields.values()
            for source in (*item.sources, *item.prerequisites)
        }

        def projection(value: Any, path: Path = ()) -> Any:
            raw = _unwrap(value)
            if isinstance(raw, OutputTemplate):
                return _OMIT
            contains_judgment = has_judgment(value)
            needed = any(path[: len(source)] == source or source[: len(path)] == path for source in required)
            if not needed and not contains_judgment:
                self.remaining[path] = value
                return _OMIT
            if isinstance(raw, Mapping):
                children = {key: projection(child, (*path, key)) for key, child in raw.items()}
                result: Any = {key: child for key, child in children.items() if child is not _OMIT}
                if not result:
                    return _OMIT
            elif isinstance(raw, list):
                child = projection(raw[0], (*path, "*"))
                result = [{} if child is _OMIT else child]
            else:
                result = raw
            return (result, *value[1:]) if isinstance(value, tuple) else result

        self.ordinary = projection(schema)
        # Compile a static topological sequence. Runtime never guesses a route.
        pending = set(self.fields)
        accepted: set[Path] = set()
        self.stages: list[list[Path] | None] = []
        ordinary_done = self.ordinary is _OMIT
        while pending:
            ready = [
                path
                for path in self.fields
                if path in pending
                and self.dependencies[path] <= accepted
                and (ordinary_done or path not in self.needs_ordinary)
            ]
            if ready:
                self.stages.append(ready)
                accepted.update(ready)
                pending.difference_update(ready)
            elif not ordinary_done:
                self.stages.append(None)
                ordinary_done = True
            else:
                raise ValueError("Judgment from_output contains a self-reference or dependency cycle.")
        if not ordinary_done:
            self.stages.append(None)
        if self.remaining:
            self.stages.append(list(self.remaining))

    def expand(self, path: Path, output: Any) -> list[Path]:
        def visit(parts: Path, current: Any, concrete: Path) -> list[Path]:
            if not parts:
                return [concrete]
            part, rest = parts[0], parts[1:]
            if part == "*":
                if not isinstance(current, list):
                    raise ValueError(f"Missing array at {path_text(concrete)}.")
                return [
                    target for index, value in enumerate(current) for target in visit(rest, value, (*concrete, index))
                ]
            child = current.get(part) if isinstance(current, dict) else None
            return visit(rest, child, (*concrete, part))

        return visit(path, output, ())

    def bind(self, field: JudgmentField, concrete: Path, output: Any) -> Any:
        if not field.sources:
            return None
        names = (
            [field.declaration.from_output]
            if isinstance(field.declaration.from_output, str)
            else field.declaration.from_output or ()
        )
        values = {
            name: self._bind_source(field, source, concrete, output) for name, source in zip(names, field.sources)
        }
        return next(iter(values.values())) if isinstance(field.declaration.from_output, str) else values

    def _bind_source(self, field: JudgmentField, source: Path, concrete: Path, output: Any) -> Any:
        bound = list(source)
        for index, part in enumerate(bound):
            if part == "*" and field.path[: index + 1] == source[: index + 1]:
                bound[index] = concrete[index]
        sentinel = object()
        value = DataLocator.locate_path_in_dict(output, path_text(tuple(bound)), default=sentinel)
        if value is sentinel:
            raise ValueError(f"Missing required output dependency: {path_text(source)}.")
        canonical = tuple("*" if isinstance(part, int) else part for part in source)
        declared = _unwrap(self.nodes[canonical])
        if not isinstance(declared, (Mapping, list, OutputTemplate)):
            adapter = TypeAdapter(declared) if not isinstance(declared, str) else None

            def validate(item: Any, dimensions: int) -> None:
                if dimensions:
                    for child in item:
                        validate(child, dimensions - 1)
                elif adapter is not None:
                    adapter.validate_python(item)

            validate(value, sum(part == "*" for part in bound))
        return value

    def validate_sources(self, output: Any) -> None:
        for path, field in self.fields.items():
            for source in (*field.sources, *field.prerequisites):
                canonical = tuple("*" if isinstance(part, int) else part for part in source)
                if any(other[: len(canonical)] == canonical for other in self.fields):
                    continue
                for concrete in self.expand(path, output):
                    self._bind_source(field, source, concrete, output)

    def validate_values(self, output: Any) -> None:
        for field in self.fields.values():
            for concrete in self.expand(field.path, output):
                value = output if not concrete else DataLocator.locate_path_in_dict(output, path_text(concrete))
                TypeAdapter(field.declaration.to_schema()[0]).validate_python(value)


def merge_output(target: Any, incoming: Any) -> Any:
    """Merge disjoint projections without allowing a model to overwrite a judgment."""
    if isinstance(target, dict) and isinstance(incoming, dict):
        for key, value in incoming.items():
            if key in target:
                target[key] = merge_output(target[key], value)
            else:
                target[key] = value
        return target
    if target is None:
        return incoming
    raise ValueError("Output projections overlap; refusing to overwrite an accepted value.")
