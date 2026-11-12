"""STT -> Agent answer -> TTS, with separate model configurations.

Set AUDIO_FILE, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY (optional),
AUDIO_BASE_URL, STT_MODEL, TTS_MODEL, AUDIO_API_KEY (optional), AUDIO_VOICE.
AUDIO_PROVIDER defaults to OpenAICompatible; use OMLX for its audio driver.
Expected key output from one real Chinese question about the daytime Moon:
The answer explained reflected sunlight and the Moon's position; speech was
returned as non-empty audio/wav. Voice/model quality is provider-dependent.
The script explicitly saves the audio; say() itself never plays or writes it.
"""
import asyncio
import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from agently import Agently


async def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    agent = Agently.create_agent()
    agent.set_settings("llm", {
        "provider": "OpenAICompatible", "base_url": os.environ["LLM_BASE_URL"],
        "api_key": os.getenv("LLM_API_KEY", ""), "model": os.environ["LLM_MODEL"],
    })
    connection = {
        "provider": os.getenv("AUDIO_PROVIDER", "OpenAICompatible"),
        "base_url": os.environ["AUDIO_BASE_URL"], "api_key": os.getenv("AUDIO_API_KEY", ""),
    }
    agent.set_settings("stt", {**connection, "model": os.environ["STT_MODEL"]})
    agent.set_settings("tts", {
        **connection, "model": os.environ["TTS_MODEL"],
        "request_options": {"voice": os.environ["AUDIO_VOICE"]},
    })
    execution = agent.input(file=os.environ["AUDIO_FILE"], type="audio").instruct("用一句简短中文回答问题。")
    speech = await execution.async_say(scope="final")
    print(await execution.async_get_text())
    if speech is not None:
        destination = Path(os.getenv("SPEECH_FILE", "answer.wav"))
        destination.write_bytes(speech.data)
        print(destination, speech.media_type, len(speech.data))


if __name__ == "__main__":
    asyncio.run(main())
