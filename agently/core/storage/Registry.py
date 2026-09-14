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

from __future__ import annotations

import inspect
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, cast

from agently.types.data import SnapshotRetentionPolicy
from agently.types.plugins import (
    DBStoreProvider,
    EmbeddingProvider,
    IngestionProfile,
    RerankProvider,
    VectorStoreProvider,
    RecordStoreBackend,
    RecordStoreBackendProvider,
    RecordStoreProviderFactory,
)

from .Errors import RecordStoreConfigurationError
from .RecordStore import RecordStore
from .LocalRecordStore import LocalRecordStore
from .Profiles import CheckpointIngestionProfile, FastIngestionProfile
from .Stores import (
    AgentEmbeddingProvider,
    AgentRerankProvider,
    CallableEmbeddingProvider,
    CallableRerankProvider,
    ChromaVectorStoreProvider,
    EmbeddingFunction,
    SQLiteVectorStoreProvider,
)


class RecordStoreRegistry:
    """Factory and registry for RecordStore foundation capabilities."""

    _DB_STORE_REQUIRED_METHODS = (
        "put_record",
        "get_record",
        "index_record",
        "search",
        "link",
        "link_evidence",
        "links",
        "checkpoint",
        "put_checkpoint",
        "get_checkpoint",
        "put_artifact_ref",
        "claim_lease",
        "heartbeat_lease",
        "release_lease",
        "put_snapshot",
        "get_snapshot",
        "latest_snapshot",
        "delete_snapshot",
        "prune_snapshots",
        "latest_checkpoint",
        "checkpoint_history",
        "append_runtime_event",
        "query_runtime_events",
    )
    _VECTOR_STORE_REQUIRED_METHODS = ("index_record", "search_by_embedding", "delete_records")

    @staticmethod
    def _missing_required_callables(candidate: Any, required: Sequence[str]) -> list[str]:
        return [name for name in required if not callable(getattr(candidate, name, None))]

    def __init__(self):
        self._profiles: dict[str, IngestionProfile] = {}
        self._backend_providers: dict[str, RecordStoreBackendProvider] = {}
        self._db_store_providers: dict[str, RecordStoreProviderFactory] = {}
        self._embedding_providers: dict[str, RecordStoreProviderFactory] = {}
        self._rerank_providers: dict[str, RecordStoreProviderFactory] = {}
        self._vector_store_providers: dict[str, RecordStoreProviderFactory] = {}
        self.register_db_store_provider("sqlite", lambda **options: None)
        self.register_embedding_provider("callable", self._create_callable_embedding_provider)
        self.register_embedding_provider("agent", self._create_agent_embedding_provider)
        self.register_rerank_provider("callable", self._create_callable_rerank_provider)
        self.register_rerank_provider("agent", self._create_agent_rerank_provider)
        self.register_vector_store_provider(
            "sqlite",
            lambda **options: SQLiteVectorStoreProvider(
                Path(options["root"]) / "records.db",
                read_only=options.get("mode") in {"read", "read_only", "readonly"},
                create=bool(options.get("create", True)),
                similarity=options.get("similarity", "cosine"),
            ),
        )
        self.register_vector_store_provider(
            "chroma",
            lambda **options: ChromaVectorStoreProvider(
                Path(options["root"]) / "vectors" / "chroma",
                create=bool(options.get("create", True)),
                mode=str(options.get("mode", "read_write")),
                similarity=options.get("similarity", "cosine"),
                collection_name=str(options.get("collection_name", "record_store_records")),
            ),
        )
        self.register_profile("fast", FastIngestionProfile())
        self.register_profile("checkpoint", CheckpointIngestionProfile())

    @staticmethod
    def _create_callable_rerank_provider(**options: Any) -> CallableRerankProvider:
        rerank_function = options.get("rerank_function") or options.get("reranker") or options.get("callable")
        if not callable(rerank_function):
            raise RecordStoreConfigurationError(
                "RecordStore callable rerank provider requires rerank_options={'rerank_function': callable}."
            )
        return CallableRerankProvider(cast(Callable[..., Any], rerank_function))

    @staticmethod
    def _create_agent_rerank_provider(**options: Any) -> AgentRerankProvider:
        agent = options.get("agent") or options.get("rerank_agent")
        if agent is None:
            raise RecordStoreConfigurationError(
                "RecordStore agent rerank provider requires rerank_options={'agent': rerank_agent}."
            )
        return AgentRerankProvider(agent)

    @staticmethod
    def _create_callable_embedding_provider(**options: Any) -> CallableEmbeddingProvider:
        embedding_function = options.get("embedding_function") or options.get("embedder") or options.get("callable")
        if not callable(embedding_function):
            raise RecordStoreConfigurationError(
                "RecordStore callable embedding provider requires embedding_options={'embedding_function': callable}."
            )
        return CallableEmbeddingProvider(cast(EmbeddingFunction, embedding_function))

    @staticmethod
    def _create_agent_embedding_provider(**options: Any) -> AgentEmbeddingProvider:
        agent = options.get("agent") or options.get("embedding_agent")
        if agent is None:
            raise RecordStoreConfigurationError(
                "RecordStore agent embedding provider requires embedding_options={'agent': embedding_agent}."
            )
        return AgentEmbeddingProvider(agent)

    def create(
        self,
        path_or_backend: str | Path | RecordStoreBackend | None = None,
        *,
        create: bool = True,
        mode: str = "read_only",
        provider: str | None = None,
        provider_options: dict[str, Any] | None = None,
        db_store_provider: Any | None = None,
        db_store_options: dict[str, Any] | None = None,
        embedding_provider: Any | None = None,
        embedding_options: dict[str, Any] | None = None,
        rerank_provider: Any | None = None,
        rerank_options: dict[str, Any] | None = None,
        vector_store_provider: Any | None = None,
        vector_store_options: dict[str, Any] | None = None,
        default_scope: dict[str, Any] | None = None,
        default_search_scope: dict[str, Any] | None = None,
        snapshot_retention: SnapshotRetentionPolicy | None = None,
    ) -> RecordStore:
        return RecordStore(
            path_or_backend,
            self,
            create=create,
            mode=mode,
            provider=provider,
            provider_options=provider_options,
            db_store_provider=db_store_provider,
            db_store_options=db_store_options,
            embedding_provider=embedding_provider,
            embedding_options=embedding_options,
            rerank_provider=rerank_provider,
            rerank_options=rerank_options,
            vector_store_provider=vector_store_provider,
            vector_store_options=vector_store_options,
            default_scope=default_scope,
            default_search_scope=default_search_scope,
            snapshot_retention=snapshot_retention,
        )

    def _materialize_record_store(
        self,
        path_or_backend: str | Path | RecordStoreBackend | None = None,
        *,
        create: bool = True,
        mode: str = "read_write",
        provider: str | None = None,
        provider_options: dict[str, Any] | None = None,
        db_store_provider: Any | None = None,
        db_store_options: dict[str, Any] | None = None,
        embedding_provider: Any | None = None,
        embedding_options: dict[str, Any] | None = None,
        rerank_provider: Any | None = None,
        rerank_options: dict[str, Any] | None = None,
        vector_store_provider: Any | None = None,
        vector_store_options: dict[str, Any] | None = None,
        default_scope: dict[str, Any] | None = None,
        default_search_scope: dict[str, Any] | None = None,
    ) -> RecordStore:
        component_options_used = any(
            value is not None
            for value in (
                db_store_provider,
                db_store_options,
                embedding_provider,
                embedding_options,
                rerank_provider,
                rerank_options,
                vector_store_provider,
                vector_store_options,
            )
        )
        if provider is not None:
            if component_options_used:
                raise RecordStoreConfigurationError(
                    "RecordStore provider=... replaces the full backend and cannot be combined with "
                    "db_store_provider, embedding_provider, rerank_provider, or vector_store_provider options."
                )
            backend = self._create_backend_from_provider(
                provider,
                root=path_or_backend,
                create=create,
                mode=mode,
                provider_options=provider_options,
            )
        elif hasattr(path_or_backend, "put") and hasattr(path_or_backend, "search"):
            if component_options_used:
                raise RecordStoreConfigurationError(
                    "A concrete RecordStoreBackend cannot be combined with component provider options."
                )
            backend = cast(RecordStoreBackend, path_or_backend)
        else:
            if path_or_backend is None:
                path_or_backend = Path.cwd() / ".agently"
            backend_root = cast(str | Path, path_or_backend)
            backend = LocalRecordStore(
                backend_root,
                create=create,
                mode=mode,
                initialize_default_vector_store_provider=False,
            )
            self._configure_local_backend_components(
                backend,
                root=backend_root,
                create=create,
                mode=mode,
                db_store_provider=db_store_provider,
                db_store_options=db_store_options,
                embedding_provider=embedding_provider,
                embedding_options=embedding_options,
                rerank_provider=rerank_provider,
                rerank_options=rerank_options,
                vector_store_provider=vector_store_provider,
                vector_store_options=vector_store_options,
            )
        return RecordStore(
            cast(RecordStoreBackend, backend),
            self,
            mode=mode,
            default_scope=default_scope,
            default_search_scope=default_search_scope,
        )

    def _configure_local_backend_components(
        self,
        backend: LocalRecordStore,
        *,
        root: str | Path | RecordStoreBackend,
        create: bool,
        mode: str,
        db_store_provider: Any | None,
        db_store_options: dict[str, Any] | None,
        embedding_provider: Any | None,
        embedding_options: dict[str, Any] | None,
        rerank_provider: Any | None,
        rerank_options: dict[str, Any] | None,
        vector_store_provider: Any | None,
        vector_store_options: dict[str, Any] | None,
    ) -> None:
        db_store_name = str(db_store_provider or "sqlite") if isinstance(db_store_provider, (str, type(None))) else str(
            getattr(db_store_provider, "name", type(db_store_provider).__name__)
        )
        if isinstance(db_store_provider, str) and db_store_name not in self._db_store_providers:
            raise RecordStoreConfigurationError(
                f"RecordStore DB store provider is not registered: { db_store_name }"
            )
        vector_store_name = (
            str(vector_store_provider)
            if isinstance(vector_store_provider, str)
            else getattr(vector_store_provider, "name", None)
        )
        if (
            isinstance(vector_store_provider, str)
            and vector_store_name != "auto"
            and vector_store_name not in self._vector_store_providers
        ):
            raise RecordStoreConfigurationError(
                f"RecordStore vector store provider is not registered: { vector_store_name }"
            )
        if isinstance(embedding_provider, str) and embedding_provider not in self._embedding_providers:
            raise RecordStoreConfigurationError(
                f"RecordStore embedding provider is not registered: { embedding_provider }"
            )
        if isinstance(rerank_provider, str) and rerank_provider not in self._rerank_providers:
            raise RecordStoreConfigurationError(
                f"RecordStore rerank provider is not registered: { rerank_provider }"
            )
        backend.configure_component_loaders(
            db_store_provider_loader=lambda: self._resolve_db_store_provider(
                db_store_provider,
                root=root,
                create=create,
                mode=mode,
                options=db_store_options,
            ),
            embedding_provider_loader=lambda: self._resolve_embedding_provider(
                embedding_provider,
                embedding_options,
            ),
            rerank_provider_loader=lambda: self._resolve_rerank_provider(
                rerank_provider,
                rerank_options,
            ),
            vector_store_provider_loader=lambda: self._resolve_vector_store_provider(
                vector_store_provider,
                root=root,
                create=create,
                mode=mode,
                options=vector_store_options,
            ),
            db_store_provider_name=db_store_name,
            vector_store_provider_name=(
                str(vector_store_name) if vector_store_name is not None else None
            ),
        )

    def _resolve_db_store_provider(
        self,
        provider: Any | None,
        *,
        root: str | Path | RecordStoreBackend,
        create: bool,
        mode: str,
        options: dict[str, Any] | None,
    ) -> tuple[DBStoreProvider | None, str]:
        if provider is not None and not isinstance(provider, str):
            resolved_provider = self._validate_db_store_provider(provider, label=getattr(provider, "name", None))
            return resolved_provider, str(getattr(resolved_provider, "name", type(resolved_provider).__name__))
        normalized = str(provider or "sqlite").strip() or "sqlite"
        if normalized not in self._db_store_providers:
            raise RecordStoreConfigurationError(f"RecordStore DB store provider is not registered: { normalized }")
        resolved = self._db_store_providers[normalized](root=root, create=create, mode=mode, **dict(options or {}))
        if resolved is None or resolved == "sqlite":
            return None, normalized
        return self._validate_db_store_provider(resolved, label=normalized), normalized

    def _validate_db_store_provider(self, candidate: Any, *, label: str | None = None) -> DBStoreProvider:
        provider_label = label or getattr(candidate, "name", None) or type(candidate).__name__
        missing = self._missing_required_callables(candidate, self._DB_STORE_REQUIRED_METHODS)
        if missing:
            raise TypeError(
                f"RecordStore DB store provider '{ provider_label }' must implement DBStoreProvider; "
                f"missing: {', '.join(missing)}."
            )
        return cast(DBStoreProvider, candidate)

    def _resolve_embedding_provider(
        self,
        provider: Any | None,
        options: dict[str, Any] | None,
    ) -> EmbeddingProvider | None:
        if provider is None:
            return None
        if callable(getattr(provider, "embed_texts", None)):
            return cast(EmbeddingProvider, provider)
        if callable(provider) and not isinstance(provider, str):
            return CallableEmbeddingProvider(cast(EmbeddingFunction, provider))
        normalized = str(provider).strip()
        if normalized not in self._embedding_providers:
            raise RecordStoreConfigurationError(f"RecordStore embedding provider is not registered: { normalized }")
        resolved = self._embedding_providers[normalized](**dict(options or {}))
        if not callable(getattr(resolved, "embed_texts", None)):
            raise TypeError(f"RecordStore embedding provider '{ normalized }' must implement embed_texts(...).")
        return cast(EmbeddingProvider, resolved)

    def _resolve_rerank_provider(
        self,
        provider: Any | None,
        options: dict[str, Any] | None,
    ) -> RerankProvider | None:
        if provider is None:
            return None
        if callable(getattr(provider, "rank", None)):
            return cast(RerankProvider, provider)
        if callable(provider) and not isinstance(provider, str):
            return CallableRerankProvider(cast(Callable[..., Any], provider))
        normalized = str(provider).strip()
        if normalized not in self._rerank_providers:
            raise RecordStoreConfigurationError(f"RecordStore rerank provider is not registered: { normalized }")
        resolved = self._rerank_providers[normalized](**dict(options or {}))
        if not callable(getattr(resolved, "rank", None)):
            raise TypeError(f"RecordStore rerank provider '{ normalized }' must implement rank(...).")
        return cast(RerankProvider, resolved)

    def _resolve_vector_store_provider(
        self,
        provider: Any | None,
        *,
        root: str | Path | RecordStoreBackend,
        create: bool,
        mode: str,
        options: dict[str, Any] | None,
    ) -> tuple[VectorStoreProvider | None, str | None, str | None]:
        if provider is not None and not isinstance(provider, str):
            vector_store_provider_object = self._validate_vector_store_provider(
                provider,
                label=getattr(provider, "name", None),
            )
            return (
                vector_store_provider_object,
                getattr(vector_store_provider_object, "name", type(vector_store_provider_object).__name__),
                None,
            )
        normalized = str(provider or "auto").strip() or "auto"
        if normalized == "auto":
            try:
                return self._create_vector_store_provider(
                    "chroma",
                    root=root,
                    create=create,
                    mode=mode,
                    options=options,
                ), "chroma", None
            except Exception as error:
                fallback = self._create_vector_store_provider(
                    "sqlite",
                    root=root,
                    create=create,
                    mode=mode,
                    options=options,
                )
                return fallback, "sqlite", f"chroma_unavailable:{type(error).__name__}"
        return self._create_vector_store_provider(
            normalized,
            root=root,
            create=create,
            mode=mode,
            options=options,
        ), normalized, None

    def _create_vector_store_provider(
        self,
        name: str,
        *,
        root: str | Path | RecordStoreBackend,
        create: bool,
        mode: str,
        options: dict[str, Any] | None,
    ) -> VectorStoreProvider:
        normalized = str(name).strip()
        if normalized not in self._vector_store_providers:
            raise RecordStoreConfigurationError(f"RecordStore vector store provider is not registered: { normalized }")
        resolved = self._vector_store_providers[normalized](
            root=root,
            create=create,
            mode=mode,
            **dict(options or {}),
        )
        return self._validate_vector_store_provider(resolved, label=normalized)

    def _validate_vector_store_provider(self, candidate: Any, *, label: str | None = None) -> VectorStoreProvider:
        provider_label = label or getattr(candidate, "name", None) or type(candidate).__name__
        missing = self._missing_required_callables(candidate, self._VECTOR_STORE_REQUIRED_METHODS)
        if missing:
            raise TypeError(
                f"RecordStore vector store provider '{ provider_label }' must implement VectorStoreProvider; "
                f"missing: {', '.join(missing)}."
            )
        if not inspect.iscoroutinefunction(getattr(candidate, "delete_records")):
            raise TypeError(
                f"RecordStore vector store provider '{ provider_label }' must implement async delete_records(...)."
            )
        return cast(VectorStoreProvider, candidate)

    def _validate_backend(self, backend: Any, *, provider: str | None = None) -> RecordStoreBackend:
        required = ("put", "search", "get_data", "capabilities")
        missing = self._missing_required_callables(backend, required)
        if missing:
            detail = f" from provider '{provider}'" if provider else ""
            raise TypeError(
                f"RecordStore backend{detail} must implement RecordStoreBackend; "
                f"missing: {', '.join(missing)}."
            )
        return cast(RecordStoreBackend, backend)

    def _create_backend_from_provider(
        self,
        provider: str,
        *,
        root: str | Path | RecordStoreBackend | None = None,
        create: bool = True,
        mode: str = "read_write",
        provider_options: dict[str, Any] | None = None,
    ) -> RecordStoreBackend:
        normalized = str(provider).strip()
        if not normalized:
            raise ValueError("RecordStore backend provider name must be non-empty.")
        if normalized not in self._backend_providers:
            raise RecordStoreConfigurationError(f"RecordStore backend provider is not registered: { normalized }")
        options = dict(provider_options or {})
        if root is not None:
            options.setdefault("root", root)
        backend = self._backend_providers[normalized](
            create=create,
            mode=mode,
            **options,
        )
        return self._validate_backend(backend, provider=normalized)

    def register_backend_provider(self, name: str, provider: RecordStoreBackendProvider):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("RecordStore backend provider name must be non-empty.")
        if not callable(provider):
            raise TypeError("RecordStore backend provider must be callable.")
        self._backend_providers[normalized] = provider
        return self

    def unregister_backend_provider(self, name: str):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("RecordStore backend provider name must be non-empty.")
        self._backend_providers.pop(normalized, None)
        return self

    def list_backend_providers(self) -> list[str]:
        return sorted(self._backend_providers.keys())

    def register_db_store_provider(self, name: str, provider: RecordStoreProviderFactory):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("RecordStore DB store provider name must be non-empty.")
        if not callable(provider):
            raise TypeError("RecordStore DB store provider must be callable.")
        self._db_store_providers[normalized] = provider
        return self

    def unregister_db_store_provider(self, name: str):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("RecordStore DB store provider name must be non-empty.")
        self._db_store_providers.pop(normalized, None)
        return self

    def list_db_store_providers(self) -> list[str]:
        return sorted(self._db_store_providers.keys())

    def register_embedding_provider(self, name: str, provider: RecordStoreProviderFactory):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("RecordStore embedding provider name must be non-empty.")
        if not callable(provider):
            raise TypeError("RecordStore embedding provider must be callable.")
        self._embedding_providers[normalized] = provider
        return self

    def unregister_embedding_provider(self, name: str):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("RecordStore embedding provider name must be non-empty.")
        self._embedding_providers.pop(normalized, None)
        return self

    def list_embedding_providers(self) -> list[str]:
        return sorted(self._embedding_providers.keys())

    def register_rerank_provider(self, name: str, provider: RecordStoreProviderFactory):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("RecordStore rerank provider name must be non-empty.")
        if not callable(provider):
            raise TypeError("RecordStore rerank provider must be callable.")
        self._rerank_providers[normalized] = provider
        return self

    def unregister_rerank_provider(self, name: str):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("RecordStore rerank provider name must be non-empty.")
        self._rerank_providers.pop(normalized, None)
        return self

    def list_rerank_providers(self) -> list[str]:
        return sorted(self._rerank_providers.keys())

    def register_vector_store_provider(self, name: str, provider: RecordStoreProviderFactory):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("RecordStore vector store provider name must be non-empty.")
        if not callable(provider):
            raise TypeError("RecordStore vector store provider must be callable.")
        self._vector_store_providers[normalized] = provider
        return self

    def unregister_vector_store_provider(self, name: str):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("RecordStore vector store provider name must be non-empty.")
        self._vector_store_providers.pop(normalized, None)
        return self

    def list_vector_store_providers(self) -> list[str]:
        return sorted(self._vector_store_providers.keys())

    def register_profile(self, name: str, handler: IngestionProfile | Callable[..., Any]):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("RecordStore profile name must be non-empty.")
        if not hasattr(handler, "ingest"):
            raise TypeError("RecordStore put profile handler is invalid.")
        self._profiles[normalized] = handler  # type: ignore[assignment]
        return self

    def get_profile(self, name: str) -> IngestionProfile:
        normalized = str(name or "fast").strip() or "fast"
        if normalized not in self._profiles:
            raise RecordStoreConfigurationError(f"RecordStore put profile is not registered: { normalized }")
        return self._profiles[normalized]

    def list_profiles(self) -> list[str]:
        return sorted(self._profiles.keys())
