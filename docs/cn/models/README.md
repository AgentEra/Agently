# Models

建议阅读顺序：

1. [模型概览](overview.md)：先理解 `OpenAICompatible`、`OpenAIResponsesCompatible`、`AnthropicCompatible` 的边界。
2. [OpenAICompatible](openai-compatible.md)：Chat Completions 兼容端点。
3. [AnthropicCompatible](anthropic-compatible.md)：Claude / Anthropic Messages API。
4. [Providers](providers/)：按 provider 填 `base_url`、`api_key`、`model`。
5. [音频](audio.md)：独立 TTS/STT、Agent 挂载与流式边界（4.1.4.8 开发版）。

具体模型名会随 provider 更新，配置时以 provider 官方文档和你的账号可用列表为准。

开发中 API 参见[独立模型用途与多模态串联](capabilities.md)。
