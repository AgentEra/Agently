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

from agently_stage import default_stage_call_bridge


import asyncio

from typing import Any, Callable
from agently.core import BaseAgent
from agently.utils import GeneratorConsumer


class KeyWaiterExtension(BaseAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__when_handlers = {}

        self.get_key_result = default_stage_call_bridge.as_sync(self.async_get_key_result)
        self.when_key = self.on_key

    def __check_keys_in_output(
        self,
        keys: list[str],
        must_in_prompt: bool = False,
    ):
        if "output" not in self.request.prompt or not self.request.prompt["output"]:
            raise NotImplementedError(
                f"Error: Cannot find keys without 'output' prompt definition.\nPrompt: { self.request.prompt }"
            )
        if must_in_prompt:
            no_found_keys = []
            for key in keys:
                if key not in self.request.prompt:
                    no_found_keys.append(key)
            if no_found_keys:
                raise NotImplementedError(
                    f"Error: Cannot wait key/keys { no_found_keys } because they were not in 'output' prompt\nPrompt: { self.request.prompt }"
                )

    def __get_consumer(self):
        result = self.get_result()
        return GeneratorConsumer(result.get_async_generator(type="instant"))

    async def async_get_key_result(
        self,
        key: str,
        *,
        must_in_prompt: bool = False,
    ):
        self.__check_keys_in_output(
            [key],
            must_in_prompt=must_in_prompt,
        )
        consumer = self.__get_consumer()

        async for data in consumer.get_async_generator():
            if key == data.path and data.is_complete:
                return data.value

    async def async_wait_keys(
        self,
        keys: list[str],
        *,
        must_in_prompt: bool = False,
    ):
        self.__check_keys_in_output(
            keys,
            must_in_prompt=must_in_prompt,
        )
        consumer = self.__get_consumer()

        async for data in consumer.get_async_generator():
            if data.path in keys and data.is_complete:
                yield data.path, data.value

    def wait_keys(
        self,
        keys: list[str],
        *,
        must_in_prompt: bool = False,
    ):
        self.__check_keys_in_output(
            keys,
            must_in_prompt=must_in_prompt,
        )
        consumer = self.__get_consumer()

        for data in consumer.get_generator():
            if data.path in keys and data.is_complete:
                yield data.path, data.value

    def on_key(self, key: str, handler: Callable[[Any], Any]):
        if key not in self.__when_handlers:
            self.__when_handlers.update({key: []})
        self.__when_handlers[key].append(handler)
        return self

    async def async_start_waiter(self, *, must_in_prompt: bool = False):
        if not self.__when_handlers:
            raise NotImplementedError(
                f"Use .when_key(<key>, <handler>) to provide at least one key handler before .start_waiter()."
            )
        handler_keys = list(self.__when_handlers.keys())
        self.__check_keys_in_output(
            handler_keys,
            must_in_prompt=must_in_prompt,
        )
        consumer = self.__get_consumer()
        tasks = []

        async def handler_wrapper(path: str, value: Any, handler: Callable[[Any], Any]) -> Any:
            return path, value, await default_stage_call_bridge.as_async(handler)(value)

        async for data in consumer.get_async_generator():
            if data.path in handler_keys and data.is_complete:
                for handler in self.__when_handlers[data.path]:
                    tasks.append(asyncio.create_task(handler_wrapper(data.path, data.value, handler)))

        self.request.prompt.clear()

        return await asyncio.gather(*tasks)

    def start_waiter(self, *, must_in_prompt: bool = False):
        if not self.__when_handlers:
            raise NotImplementedError(
                f"Use .when_key(<key>, <handler>) to provide at least one key handler before .start_waiter()."
            )
        handler_keys = list(self.__when_handlers.keys())
        self.__check_keys_in_output(
            handler_keys,
            must_in_prompt=must_in_prompt,
        )
        consumer = self.__get_consumer()
        results = []

        for data in consumer.get_generator():
            if data.path in handler_keys and data.is_complete:
                for handler in self.__when_handlers[data.path]:
                    results.append(
                        (
                            data.path,
                            data.value,
                            default_stage_call_bridge.as_sync(
                                handler,
                            )(data.value),
                        )
                    )

        self.request.prompt.clear()

        return results
