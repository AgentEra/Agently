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

import yaml
from typing import TYPE_CHECKING, Any

from agently.types.data import AgentlyRequestData, AgentlyRequestDataDict
from agently.utils import DataFormatter

from ...function_tools import FunctionTools


class OpenAICompatibleRequestBuilderMixin(FunctionTools):
    name: str
    model_type: str
    plugin_settings: Any
    prompt: Any

    if TYPE_CHECKING:
        def _get_http_timeout(self, *, disable_read: bool = False) -> Any: ...

    def _rerank_request_data(self) -> dict[str, Any]:
        """Build the Cohere/Jina-shaped /rerank body and fail before any network call."""
        sanitized_input = DataFormatter.sanitize(self.prompt["input"])
        if not isinstance(sanitized_input, dict):
            raise ValueError(
                f"Plugin Name: { self.name }\n"
                "Error: model_type='rerank' requires input as "
                "{'query': <str>, 'documents': [<str> | {'text': <str>}]}."
            )
        query = sanitized_input.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"Plugin Name: { self.name }\n" "Error: rerank input 'query' must be a non-empty string."
            )
        raw_documents = sanitized_input.get("documents")
        if not isinstance(raw_documents, list) or len(raw_documents) == 0:
            raise ValueError(
                f"Plugin Name: { self.name }\n" "Error: rerank input 'documents' must be a non-empty list."
            )
        texts: list[str] = []
        has_dict_document = False
        for index, item in enumerate(raw_documents):
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                has_dict_document = True
                texts.append(item["text"])
            else:
                raise ValueError(
                    f"Plugin Name: { self.name }\n"
                    f"Error: rerank document at index { index } must be a string "
                    "or a dictionary carrying a 'text' string."
                )
        # Rerank services accept a homogeneous document list, so a mixed caller list is
        # normalized to one shape instead of being forwarded and rejected by the service.
        documents: list[Any] = [{"text": text} for text in texts] if has_dict_document else list(texts)
        return {"query": query, "documents": documents}

    def generate_request_data(self) -> "AgentlyRequestData":
        agently_request_dict: AgentlyRequestDataDict = {
            "client_options": {},
            "headers": {},
            "data": {},
            "request_options": {},
            "request_url": "",
        }
        # main data
        match self.model_type:
            case "chat":
                request_data = {
                    "messages": self.prompt.to_messages(
                        rich_content=bool(
                            self.plugin_settings.get(
                                "rich_content",
                                False,
                            )
                        ),
                        strict_role_orders=bool(
                            self.plugin_settings.get(
                                "strict_role_orders",
                                False,
                            ),
                        ),
                    )
                }
            case "completions":
                request_data = {"prompt": self.prompt.to_text()}
            case "embeddings":
                sanitized_input = DataFormatter.sanitize(self.prompt["input"])
                if isinstance(sanitized_input, list):
                    request_data = {
                        "input": [
                            (
                                str(item)
                                if isinstance(item, (str, int, float, bool)) or item is None
                                else yaml.safe_dump(item)
                            )
                            for item in sanitized_input
                        ],
                    }
                else:
                    request_data = {
                        "input": (
                            str(sanitized_input)
                            if isinstance(sanitized_input, (str, int, float, bool)) or sanitized_input is None
                            else yaml.safe_dump(sanitized_input)
                        )
                    }
            case "rerank":
                request_data = self._rerank_request_data()
            case _:
                raise TypeError(
                    f"Plugin Name: { self.name }\n" f"Error: Cannot support model type: '{ self.model_type }'"
                )
                request_data = {}
        ## set
        agently_request_dict["data"] = request_data

        # headers
        headers: dict[str, str] = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("headers"),
            value_format="str",
            default_value={},
        )
        headers.update({"Connection": "close"})
        ## set
        agently_request_dict["headers"] = headers

        # client options
        client_options = DataFormatter.to_str_key_dict(self.plugin_settings.get("client_options"), default_value={})
        client_options.setdefault("trust_env", False)
        ## proxy
        proxy = self.plugin_settings.get("proxy", None)
        if proxy:
            client_options.update({"proxy": proxy})
        ## timeout
        timeout = self._get_http_timeout()
        client_options.update({"timeout": timeout})
        ## set
        agently_request_dict["client_options"] = client_options

        # request_options
        legacy_options = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("options"),
            value_format="serializable",
            default_value={},
        )
        request_options = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("request_options"),
            value_format="serializable",
            default_value={},
        )
        # Backward compatibility for older examples/configs that still use
        # plugin-root `options` for default request-body parameters.
        request_options = {**legacy_options, **request_options}
        request_options_in_prompt = self.prompt.get("options", {})
        if request_options_in_prompt:
            request_options.update(request_options_in_prompt)
            request_options = DataFormatter.to_str_key_dict(
                request_options,
                value_format="serializable",
                default_value={},
            )
        if self.model_type == "chat":
            prompt_tools = self._prompt_tool_functions(self.prompt.get("tools"))
            explicit_tools = request_options.get("tools")
            if prompt_tools and (explicit_tools is None or isinstance(explicit_tools, list)):
                explicit_tools = explicit_tools or []
                explicit_names = set()
                for tool in explicit_tools:
                    if isinstance(tool, dict) and tool.get("type") == "function":
                        function = tool.get("function")
                        if isinstance(function, dict) and isinstance(function.get("name"), str):
                            explicit_names.add(function["name"])
                request_options["tools"] = [
                    {"type": "function", "function": function}
                    for function in prompt_tools
                    if function["name"] not in explicit_names
                ] + explicit_tools
        ## !: ensure model
        default_models = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("default_model"),
            value_format="serializable",
            default_key=self.model_type,
        )
        # There is no honest universal default rerank model, so `default_model.rerank`
        # stays empty and the missing configuration is reported before dispatch.
        default_model = (
            default_models.get("rerank") if self.model_type == "rerank" else default_models[self.model_type]
        )
        model = self.plugin_settings.get("model", default_model)
        if self.model_type == "rerank" and not (isinstance(model, str) and model.strip()):
            raise ValueError(
                f"Plugin Name: { self.name }\n"
                "Error: model_type='rerank' requires an explicit rerank model.\n"
                "Tips: set `plugins.ModelRequester.OpenAICompatible.model` or "
                "`default_model.rerank` to the model id your rerank service reports."
            )
        request_options.update({"model": model})
        ## !: ensure stream
        is_stream = self.plugin_settings.get("stream")
        if is_stream is None:
            is_stream = self.model_type not in ("embeddings", "rerank")
        if self.model_type == "rerank":
            # Cohere/Jina-shaped /rerank has no streaming form; never send `stream: true`.
            is_stream = False
        request_options.update({"stream": is_stream})
        ## set
        agently_request_dict["request_options"] = request_options

        # request url
        ## get full url
        full_url = self.plugin_settings.get("full_url")
        ## get base url
        base_url = str(self.plugin_settings.get("base_url"))
        base_url = base_url[:-1] if base_url[-1] == "/" else base_url
        ## get path mapping
        path_mapping = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("path_mapping"),
            value_format="str",
            default_value={},
        )
        path_mapping = {k: v if v[0] == "/" else f"/{ v }" for k, v in path_mapping.items()}
        ## set
        if isinstance(full_url, str):
            request_url = full_url
        else:
            request_url = f"{ base_url }{ path_mapping[self.model_type] }"
        agently_request_dict["request_url"] = request_url

        return AgentlyRequestData(**agently_request_dict)
