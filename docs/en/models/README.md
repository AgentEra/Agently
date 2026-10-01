# Models

Suggested reading order:

1. [Models Overview](overview.md): understand the boundaries of `OpenAICompatible`, `OpenAIResponsesCompatible`, and `AnthropicCompatible`.
2. [OpenAICompatible](openai-compatible.md): Chat Completions compatible endpoints.
3. [AnthropicCompatible](anthropic-compatible.md): Claude / Anthropic Messages API.
4. [Providers](providers/): fill `base_url`, `api_key`, and `model` per provider.
5. [Audio](audio.md): independent TTS/STT capability, Agent binding and streaming boundaries (4.1.4.8 development).

Model ids change as providers update. Use provider docs and your account's available model list when configuring production systems.

See [Independent model uses and multimodal tasks](capabilities.md) for the development API.
