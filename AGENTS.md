# AGENTS.md — xbattle (Zero-Player EDA PoC)

## Stack
- Python 3.10+, `asyncio` + `redis.asyncio` mandatory. Only exception: `injector.py` uses blocking sync `redis` for CLI simplicity.
- Deps: `pip install -r requirements.txt` (`redis`, `openai`, `python-dotenv`, `fastapi`/`uvicorn`, `edge-tts`, `pygame-ce`). Test-only: `pip install -q fakeredis`. NOTE: `pygame` has no py3.14 wheel (tries source build, fails) — use `pygame-ce` (drop-in, `import pygame` works).

## Services
- Redis must be on `localhost:6379`. No Redis in this repo; Docker daemon is usually OFF on this Windows box — do not assume `docker run` works. Verify with `python -c "import redis.asyncio,asyncio; print(asyncio.run(redis.asyncio.Redis(decode_responses=True).ping()))"`.
- No Docker/CI/lint/typecheck config. Only verification is `python -m py_compile *.py` + fakeredis mocks (see below).

## Architecture (Redis Pub/Sub, JSON, `decode_responses=True` always)
- `cmd.interact`: `{"action":"donate|recruit|heal|rally|curse|omen","target":"A|B","value":int}` — produced by `injector.py`, consumed by `engine.py`. `value` só relevante p/ `donate` (>0, senão ignorado).
- `game.state`: `{"tick","camp_tick","era","base_A_gold","base_B_gold","base_A_hp","base_B_hp","march_A","march_B","favor_A","favor_B","wins_A","wins_B","game_over","restart_in"}` — todo tick (`TICK_RATE=1.0s`).
- `game.events`: `{"tick":int,"event_msg":str,"kind"?}` — spawn + donate + hit + vitória + era + omen + momentum + campanha.
- `narrator.broadcast`: `{"text":str,"timestamp":int}` — produced by `cronista.py` only.
- Run order: Redis → `python engine.py` → `python cronista.py` → `python injector.py` + `uvicorn gateway:app --port 8000` for the viewer (open `http://localhost:8000`) + `python locutor.py` (TTS p/ OBS) — 5 processos/terminais.

## Entrypoints / ownership
- `engine.py`: `tick_loop(pub)` (sole writer, 1 tps, spawn at gold>=`SPAWN_AT`=100 then reset, ~100s natural spawn) + `cmd_listener(pub)` (sole reader). Separate Redis conns for pub vs sub; `asyncio.gather` both in `main()`. Pass `pub` as arg, never global-None. Engine real: eras `colheita/guerra/crepusculo` (`1200/3000/3600` ticks), `MARCH_TICKS=25`, `SIEGE_DMG=5`, `CASTLE_HP=100`, morte súbita pós-crepúsculo, `favor/wins`, momentum (3 hits), `rally/freeze/eclipse`, `omen` idle 600s, `eclipse_until` zerado no `_restart`, donate `value<=0` ignorado, empate na morte súbita decidido por `favor` (sorteio se igual).
- `injector.py`: blocking `input()` loop, parses `"A 50"` (legado = donate) + `"<ACTION> <TARGET> [VALUE]"` (`donate|recruit|heal|rally|curse|omen`). No asyncio here. `donate` com `value<=0` rejeitado no CLI e ignorado no engine.
- `cronista.py`: `event_listener()` appends to global `event_buffer` and sets `flush_signal` on critical (spawn) or `len >= CRONISTA_THRESHOLD` (default 5); `chronicler_loop(pub)` uses `asyncio.wait_for(signal, CRONISTA_MAX_SILENCE=10s)` — immediate flush on trigger, fallback flush on silence, skip if empty; snapshot with `list()` then `clear()` + signal `clear()` BEFORE the LLM call; `generate_commentary(events)` via `AsyncOpenAI`. Publish fallback on any exception, never crash.
- `gateway.py`: `redis_pump()` (single task, subscribes `game.state` + `game.events` + `narrator.broadcast`, envelopes `{"type","data"}`, drops zombies on send error, `aclose()` no reconnect) + `/ws` endpoint + `GET /` serving `index.html`. `index.html`: pixel-art battlefield (offscreen bg, smoothing off, 3 lanes, castles, marching soldiers from spawn events, clash marks, siege HP backend-driven via `game.state`, campaign clock, feed). Viewer is READ-ONLY — never publishes back to Redis. WS usa `location.host` (ws/wss automático).
- `locutor.py`: TTS worker, `asyncio.Queue` (producer `narrator.broadcast` → consumer serial). Voice `pt-BR-AntonioNeural` (override dinâmico via `LOCUTOR_VOICE`, alt `pt-BR-FranciscaNeural`). Each item gets a unique `tts_<uuid>.mp3` (prefix dinâmico via `LOCUTOR_TEMP_PREFIX`) — never overwrite a file under playback; after play, `mixer.music.unload()` then `os.remove`; partials deleted on failure, stale `tts_*.mp3` cleaned at startup. No `time.sleep()` — only `await asyncio.sleep()`. Run 4th: `python locutor.py` (needs audio output for OBS capture).

## Env / LLM (.env, loaded with optional `load_dotenv`)
- `LLM_PROVIDER` (LOCAL|CLOUD, só etiqueta informativa — não lida pelo código), `LLM_BASE_URL` (default `http://localhost:11434/v1`), `LLM_API_KEY` (default `ollama`), `LLM_MODEL_NAME` (default `llama3:8b`), `LLM_MAX_TOKENS` (default `1200`). `_client()` reads env on every call — keep it dynamic for LOCAL↔CLOUD switching.
- `SYSTEM_PROMPT` (pt-BR, narrador frenético de eSports, max 3 frases, sem raciocínio exposto — clause 3 economiza tokens em modelos reasoning) is spec-frozen — do not reword. `FALLBACK_TEXT` must stay non-empty, no emojis/hashtags.
- Local LM Studio (this box): server at `http://localhost:1234/v1`, chat model `google/gemma-4-12b`, dummy key `lm-studio`. `.env` is gitignored — never commit it. List models via `GET /v1/models`.
- Reasoning models (Gemma) burn a variable 300-700+ tokens thinking: tight budgets return empty (`finish_reason=length`, text in `reasoning_content`). Default `1200` + 1 retry on empty inside `generate_commentary`; `enable_thinking:false` was probed and is ignored by this model. Verified 2026-09-08 with gemma-4-12b (3/3 non-empty).

## Gotchas
- Always ignore pubsub subscribe-acks: `if msg.get("type") != "message": continue`, plus `try/except (JSONDecodeError, TypeError)` and `(ValueError, TypeError)` on donate value.
- Listeners com auto-reconnect (`engine`/`cronista`/`gateway`/`locutor`) devem `await sub.aclose()` no `finally` — senão vaza conexão a cada queda do Redis.
- `timestamp` is `int(time.time())`, not float.
- `AsyncOpenAI(..., timeout=25.0)`, `temperature=0.9`, `max_tokens` from `LLM_MAX_TOKENS` (default 1200).
- PowerShell quoting breaks `python -c` with nested f-strings/quotes — write temp `_check_*.py` files for tests, delete after. Also delete `__pycache__` after test runs.
