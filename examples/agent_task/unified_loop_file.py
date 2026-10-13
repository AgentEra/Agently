"""Real-model file delivery through the unified Loop (artifact-delivery repair).

By default configure DEEPSEEK_API_KEY (optional DEEPSEEK_BASE_URL and
DEEPSEEK_MODEL) in the environment or .env. To use the local service explicitly,
set FILE_DEMO_PROVIDER=local and OMLX_BASE_URL, OMLX_MODEL, OMLX_API_KEY.
The model writes a short handoff note through the native file Action and selects
its observed path. Host validates existence and retention after terminal cleanup.
The note's task facts are synthetic teaching inputs, not a deployment approval.
Flow: decide -> write_file -> observation -> read_file -> decide -> retention.
This checks physical delivery, not general semantic or layout quality.
Expected key output from an installed 4.1.4.10 candidate / DeepSeek V4-Pro run:
    {"retained": True, "own_execution": True}
"""
import asyncio
import os
from tempfile import TemporaryDirectory

from dotenv import load_dotenv
from agently import Agently


async def main() -> None:
    load_dotenv()
    agent = Agently.create_agent()
    provider = os.getenv("FILE_DEMO_PROVIDER", "deepseek")
    if provider not in {"deepseek", "local"}:
        raise ValueError("FILE_DEMO_PROVIDER must be deepseek or local")
    local = provider == "local"
    agent.set_settings("plugins.ModelRequester.OpenAICompatible", {
        "base_url": os.environ["OMLX_BASE_URL"] if local else os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/"),
        "model": os.environ["OMLX_MODEL"] if local else os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        "auth": os.environ["OMLX_API_KEY" if local else "DEEPSEEK_API_KEY"], "stream": False,
        "request_retry": False,
        "request_options": {"temperature": 0, "max_tokens": 4096,
                            **({"chat_template_kwargs": {"enable_thinking": False}} if local else
                               {"thinking": {"type": "disabled"}})},
    })
    with TemporaryDirectory(prefix="agently-file-") as directory:
        agent.use_task_workspace(directory, mode="read_only")
        agent.enable_task_workspace_file_actions(read=True, write=True, search=False, list_files=False)
        execution = (
            agent.create_execution("long_task", limits={"max_model_requests": 6, "max_seconds": 120})
            .input("依据本教学合成场景写一份简短中文交接说明到 handoff.md：测试已通过，部署待负责人确认。写入后读回实际文件，再返回Action提供的完整文件路径。")
            .use_actions(["write_file", "read_file"]).require_actions(["write_file", "read_file"])
            .output({"file_path": str})
        )
        execution.options["context_budget"] = {"optional_selection": "none"}
        result = await execution.async_get_data(max_retries=0)
        path = execution.task_workspace.resolve_file_path(result["file_path"])
        assert path.is_file() and path.stat().st_size > 0
        own_execution = result["file_path"].startswith(f".agently/files/{execution.id}/")
        assert own_execution
        body = path.read_bytes()
        await execution.async_close()
        assert path.read_bytes() == body
        print({"retained": path.is_file(), "own_execution": own_execution})


if __name__ == "__main__":
    asyncio.run(main())
