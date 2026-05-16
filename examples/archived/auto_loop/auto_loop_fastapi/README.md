# Auto Loop FastAPI (SSE / WebSocket / POST)

This is a runnable FastAPI project that exposes auto_loop as:
- SSE for streaming
- WebSocket for duplex chat
- POST for single-shot replies

## Run (local)
```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Docker
```bash
docker build -t agently-auto-loop .
docker run -p 8000:8000 -e OLLAMA_BASE_URL=http://host.docker.internal:11434/v1 agently-auto-loop
```

## Endpoints
- `GET /sse?question=...` -> SSE stream
- `WS /ws` -> send text, receive stream events
- `POST /ask` -> JSON response

## Event format
Each streaming event is a JSON string:
```json
{"type": "status", "data": "planning started"}
```

Common event types:
- `status`
- `thinking_delta`
- `plan`
- `tool`
- `reply`
- `memo`
- `error`
