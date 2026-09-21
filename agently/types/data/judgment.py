"""Atomic output judgments, usable with Jev or a normal structured LLM output."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import Field


@dataclass(frozen=True)
class OutputTemplate(ABC):
    """Provider-independent output contract with explicit output dependencies."""

    question: str
    from_output: str | Sequence[str] | None = field(default=None, kw_only=True)

    after_output: str | Sequence[str] | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError("A judgment requires a non-empty question.")
        for name in ("from_output", "after_output"):
            paths = getattr(self, name)
            if paths is None:
                continue
            if isinstance(paths, str):
                if not paths.strip():
                    raise ValueError(f"{name} must be a non-empty output path.")
            elif (
                isinstance(paths, (list, tuple))
                and paths
                and all(isinstance(path, str) and path.strip() for path in paths)
            ):
                if len(set(paths)) != len(paths):
                    raise ValueError(f"{name} paths must be unique.")
                object.__setattr__(self, name, tuple(paths))
            else:
                raise ValueError(f"{name} must be a non-empty path or array of unique paths.")

    @abstractmethod
    def to_schema(self) -> Any:
        """Return (Python annotation, prompt description, True, metadata)."""
        raise NotImplementedError


@dataclass(frozen=True)
class Probability(OutputTemplate):
    """Probability of a proposition, returned as a float between zero and one."""

    def to_schema(self) -> Any:
        return (
            Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)],
            self.question,
            True,
            {"judgment": True},
        )


@dataclass(frozen=True)
class Choice(OutputTemplate):
    """Select one candidate key; descriptions explain the candidates."""

    options: Mapping[str, Any]

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.options, Mapping) or not 1 <= len(self.options) <= 255:
            raise ValueError("Choice requires between 1 and 255 named options.")
        if any(not isinstance(key, str) or not key for key in self.options):
            raise ValueError("Choice option keys must be non-empty strings.")
        if any(value is not None and not isinstance(value, (str, Mapping, list)) for value in self.options.values()):
            raise ValueError("Choice descriptions must be text, objects, arrays or null.")
        try:
            options = json.loads(json.dumps(dict(self.options), allow_nan=False))
        except (TypeError, ValueError) as error:
            raise ValueError("Choice descriptions must be JSON-compatible.") from error
        object.__setattr__(self, "options", options)

    def to_schema(self) -> Any:
        annotation = Literal[tuple(self.options)]
        return (
            annotation,
            f"{self.question}\nCandidates: {json.dumps(dict(self.options), ensure_ascii=False)}",
            True,
            {"judgment": True},
        )


@dataclass(frozen=True)
class Score(OutputTemplate):
    """Expected grade on an ordered scale (a float, not a rounded category)."""

    options: Sequence[Any]

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            isinstance(self.options, (str, bytes))
            or not isinstance(self.options, Sequence)
            or not 2 <= len(self.options) <= 10
        ):
            raise ValueError("Score requires an ordered sequence of 2 to 10 grades.")
        if any(not isinstance(value, (str, Mapping, list)) for value in self.options):
            raise ValueError("Score grades must be text, objects or arrays.")
        try:
            options = json.loads(json.dumps(list(self.options), allow_nan=False))
        except (TypeError, ValueError) as error:
            raise ValueError("Score grades must be JSON-compatible.") from error
        object.__setattr__(self, "options", tuple(options))

    def to_schema(self) -> Any:
        annotation = Annotated[float, Field(ge=0, le=len(self.options) - 1, allow_inf_nan=False, strict=True)]
        return (
            annotation,
            f"{self.question}\nOrdered grades (starting at 0): {json.dumps(list(self.options), ensure_ascii=False)}",
            True,
            {"judgment": True},
        )


__all__ = ["OutputTemplate", "Probability", "Choice", "Score"]
