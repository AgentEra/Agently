"""Real-model file delivery through the unified Loop (artifact-delivery repair).

Configure OMLX_BASE_URL, OMLX_MODEL and OMLX_API_KEY in the environment or .env.
The model writes a short handoff note through the native file Action and selects
its observed path. Host validates existence and retention after terminal cleanup.
Flow: decide -> write_file -> observed file identity -> decide -> retention.
Expected key output from a real Qwen3.8-27B-4bit run:
    {"retained": True, "private": True}
"""
import asyncio
import os
from tempfile import TemporaryDirectory

from dotenv import load_dotenv
from agently import Agently


async def main() -> None:
    load_dotenv()
    agent = Agently.create_agent()
    agent.set_settings("plugins.ModelRequester.OpenAICompatible", {
        "base_url": os.environ["OMLX_BASE_URL"], "model": os.environ["OMLX_MODEL"],
        "auth": os.environ["OMLX_API_KEY"], "stream": False,
        "request_options": {"temperature": 0, "max_tokens": 4096,
                            "chat_template_kwargs": {"enable_thinking": False}},
    })
    with TemporaryDirectory(prefix="agently-file-") as directory:
        agent.use_task_workspace(directory, mode="read_only")
        agent.enable_task_workspace_file_actions(read=True, write=True, search=False, list_files=False)
        execution = (
            agent.create_execution("long_task", limits={"max_model_requests": 6, "max_seconds": 120})
            .input("写一份简短中文交接说明到 handoff.md：测试已通过，部署待负责人确认。返回实际写入的文件路径。")
            .use_actions("write_file").require_actions("write_file")
            .output({"file_path": str})
        )
        execution.options["context_budget"] = {"optional_selection": "none"}
        result = await execution.async_get_data()
        path = execution.task_workspace.resolve_file_path(result["file_path"])
        assert path.is_file() and path.stat().st_size > 0
        print({"retained": path.is_file(), "private": ".agently/files/" in result["file_path"]})
        await execution.async_close()


if __name__ == "__main__":
    asyncio.run(main())
