# AGENTS.md — xbattle (Zero-Player EDA PoC)

## Stack
- Python 3.10+, `asyncio` + `redis.asyncio` mandatory. Only exception: `injector.py` uses blocking sync `redis` for CLI simplicity.
- Deps: `pip install -r requirements.txt` (`redis`, `openai`, `python-dotenv`). Test-only: `pip install -q fakeredis`.

## Services
- Redis must be on `localhost:6379`. No Redis in this repo; Docker daemon is usually OFF on this Windows box — do not assume `docker run` works. Verify with `python -c "import redis.asyncio,asyncio; print(asyncio.run(redis.asyncio.Redis(decode_responses=True).ping()))"`.
- No Docker/CI/lint/typecheck config. Only verification is `python -m py_compile *.py` + fakeredis mocks (see below).

## Architecture (Redis Pub/Sub, JSON, `decode_responses=True` always)
- `cmd.interact`: `{"action":"donate","target":"A|B","value":int}` — produced by `injector.py`, consumed by `engine.py`.
- `game.state`: `{"tick":int,"base_A_gold":int,"base_B_gold":int}` — every tick (0.2s).
- `game.events`: `{"tick":int,"event_msg":str}` — spawn + donate.
- `narrator.broadcast`: `{"text":str,"timestamp":int}` — produced by `cronista.py` only.
- Run order: Redis → `python engine.py` → `python cronista.py` → `python injector.py` (3 terminals).

## Entrypoints / ownership
- `engine.py`: `tick_loop(pub)` (sole writer, 5 tps, spawn at gold>=100 then reset) + `cmd_listener(pub)` (sole reader). Separate Redis conns for pub vs sub; `asyncio.gather` both in `main()`. Pass `pub` as arg, never global-None.
- `injector.py`: blocking `input()` loop, parses `"A 50"`. No asyncio here.
- `cronista.py`: `event_listener()` appends to global `event_buffer`; `chronicler_loop(pub)` sleeps 30s, snapshots with `list()` then `clear()` BEFORE the LLM call; `generate_commentary(events)` via `AsyncOpenAI`. Publish fallback on any exception, never crash.

## Env / LLM (.env, loaded with optional `load_dotenv`)
- `LLM_PROVIDER` (LOCAL|CLOUD), `LLM_BASE_URL` (default `http://localhost:11434/v1`), `LLM_API_KEY` (default `ollama`), `LLM_MODEL_NAME` (default `llama3:8b`). `_client()` reads env on every call — keep it dynamic for LOCAL↔CLOUD switching.
- `SYSTEM_PROMPT` (pt-BR, sádico/sarcástico) is spec-frozen — do not reword. `FALLBACK_TEXT` must stay non-empty, no emojis/hashtags.

## Gotchas
- Always ignore pubsub subscribe-acks: `if msg.get("type") != "message": continue`, plus `try/except (JSONDecodeError, TypeError)` and `(ValueError, TypeError)` on donate value.
- `timestamp` is `int(time.time())`, not float.
- `AsyncOpenAI(..., timeout=25.0)`, `temperature=0.9`, `max_tokens=150`.
- PowerShell quoting breaks `python -c` with nested f-strings/quotes — write temp `_check_*.py` files for tests, delete after. Also delete `__pycache__` after test runs to keep the 4-file layout.
