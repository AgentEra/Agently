import asyncio
from .utils import ComponentABC
from Agently.utils import RuntimeCtxNamespace
from openai import OpenAI
import httpx

class OpenAIAssistant(ComponentABC):
    def __init__(self, agent: object):
        self.agent = agent
        self.assistant_runtime_ctx = RuntimeCtxNamespace("openai_assistant", self.agent.agent_runtime_ctx)

    def use_assistant(self, use_assistant: bool):
        self.agent.request.set_model("use_assistant", use_assistant)
        return self

    def set_assistant_id(self, assistant_id: str):
        self.agent.request.set_model("assistant_id", assistant_id)
        self.agent.request.set_model("use_assistant", True)
        return self

    def set_name(self, name: str):
        self.assistant_runtime_ctx.set("name", name)
        return self

    def set_instruction(self, instruction: str):
        self.assistant_runtime_ctx.set("instruction", instruction)
        return self

    def set_model(self, model: str):
        self.agent.request.set_model("options.model", model)
        return self

    def set_api_key(self, api_key: str):
        self.agent.request.set_model_auth({ "api_key": api_key })
        return self

    def set_proxy(self, proxy: str):
        self.agent.request.set_proxy(proxy)
        return self

    def append_file(self, file_name: str, file_path: str):
        self.assistant_runtime_ctx.update(f"files.{ file_name }.path", file_path)
        return self

    def get_file_id(self, file_name: str):
        return self.assistant_runtime_ctx.get(f"files.{file_name}.id")

    def remove_file(self, file_name: str):
        self.assistant_runtime_ctx.remove(f"files.{ file_name }")
        return self

    def append_tool(self, tool_type: str, **kwargs):
        if { "type": tool_type, **kwargs } not in self.assistant_runtime_ctx.get("tools", []):
            self.assistant_runtime_ctx.append("tools", { "type": tool_type, **kwargs })
        return self

    def use_code_interpreter(self):
        self.append_tool("code_interpreter")
        return self

    def append_function(self, function_info: dict):
        self.append_tool(self, "function", function = function_info)
        return self

    def _create_client(self):
        client_params = {}
        base_url = self.agent.request.settings.get_trace_back("model.OpenAI.url")
        if base_url:
            client_params.update({ "base_url": base_url })
        proxy = self.agent.request.settings.get_trace_back("proxy")
        if proxy:
            client_params.update({ "http_client": httpx.Client( proxies = proxy ) })
        api_key = self.agent.request.settings.get_trace_back("model.OpenAI.auth.api_key")
        if api_key:
            client_params.update({ "api_key": api_key })
        else:
            raise Exception("[Request] OpenAI require api_key. use .set_auth({ 'api_key': '<Your-API-Key>' }) to set it.")
        client = OpenAI(**client_params)
        return client

    def update(self, *, force_to_create: bool=False):
        # create client
        client = self._create_client()
        # check file change
        files = self.assistant_runtime_ctx.get("files", {})
        file_ids = []
        for file_name, file_info in files.items():
            if "id" not in file_info:
                file = client.files.create(
                    file=open(file_info["path"], "rb"),
                    purpose="assistants",
                )
                file_info["id"] = file.id
                self.assistant_runtime_ctx.set(f"files.{ file_name }.id", file_info["id"])
            file_ids.append(file_info["id"])
        # prepare assisant info
        name = self.assistant_runtime_ctx.get("name", "My Assistant")
        instruction = self.assistant_runtime_ctx.get("instruction", "You are a helpful assistant.")
        model = self.agent.request.settings.get_trace_back(
            "model.OpenAI.options.model",
            "gpt-3.5-turbo-1106",
        )
        tools = self.assistant_runtime_ctx.get("tools", [])
        
        assistant_id = self.agent.request.settings.get_trace_back("model.OpenAI.assistant_id")
        # update
        if assistant_id and not force_to_create:
            assistant = client.beta.assistants.update(
                assistant_id = assistant_id,
                name = name,
                instructions = instruction,
                tools = tools,
                model = model,
                file_ids = file_ids
            )
        # create
        else:
            assistant = client.beta.assistants.create(
                name = name,
                instructions = instruction,
                tools = tools,
                model = model,
                file_ids = file_ids,
            )
            assistant_id = assistant.id
            self.set_assistant_id(assistant.id)
        # turn on setting 'use_assistant'
        self.agent.request.set_model("use_assistant", True)
        # and save agent to keep all information
        self.agent.save()
        return assistant_id

    def create(self):
        return self.update(force_to_create=True)

    def export(self):
        return {
            "prefix": None,
            "suffix": None,
            "alias": {},
        }

def export():
    return ("OpenAIAssistant", OpenAIAssistant)
