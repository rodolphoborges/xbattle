import asyncio
import json
import os
import time

try:  # optional .env support
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import redis.asyncio as redis
from openai import AsyncOpenAI

SYSTEM_PROMPT = "Você é um deus sádico, sarcástico e observador de uma guerra medieval fútil. Você lê os relatórios das batalhas e os narra para o público. Regras estritas: 1) Resuma o caos em no máximo 2 frases curtas. 2) Seja irônico. 3) Não use emojis ou hashtags. 4) Se os eventos forem chatos, zombe da incompetência das facções."

FALLBACK_TEXT = "Os idiotas continuaram se matando e nem isso fizeram direito."

event_buffer = []  # sliding window of game.events


def _client() -> tuple[AsyncOpenAI, str]:
    """Build AsyncOpenAI from .env (works for Ollama LOCAL and CLOUD)."""
    base_url = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    api_key = os.getenv("LLM_API_KEY", "ollama")  # Ollama ignores key
    model = os.getenv("LLM_MODEL_NAME", "llama3:8b")
    return AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=25.0), model


async def generate_commentary(events: list) -> str:
    """Task C: call LLM with flushed events, return narration text."""
    client, model = _client()
    # Reasoning models (e.g. Gemma via LM Studio) spend tokens thinking;
    # 150 cuts the answer off (finish_reason=length, empty content). 500 fits.
    try:
        max_tokens = int(os.getenv("LLM_MAX_TOKENS", "500"))
    except (ValueError, TypeError):
        max_tokens = 500
    user_prompt = f"Relatórios de batalha:\n{json.dumps(events, ensure_ascii=False)}"
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.9,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


async def event_listener():
    """Task A: subscribe game.events, append to buffer."""
    sub = redis.Redis(host="localhost", port=6379, decode_responses=True)
    ps = sub.pubsub()
    await ps.subscribe("game.events")
    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue
        try:
            event_buffer.append(json.loads(msg["data"]))
        except (json.JSONDecodeError, TypeError):
            continue


async def chronicler_loop(pub: redis.Redis):
    """Task B: every 30s flush window -> LLM -> narrator.broadcast."""
    while True:
        await asyncio.sleep(30)
        if not event_buffer:
            continue
        events = list(event_buffer)
        event_buffer.clear()  # instant clear to avoid race
        try:
            text = await generate_commentary(events)
            if not text:
                text = FALLBACK_TEXT
        except Exception as e:  # timeout / network / API error -> no crash
            print(f"[cronista] LLM failed: {e!r}, using fallback")
            text = FALLBACK_TEXT
        payload = {"text": text, "timestamp": int(time.time())}
        await pub.publish("narrator.broadcast", json.dumps(payload, ensure_ascii=False))
        print(f"[cronista] broadcast: {payload}")


async def main():
    pub = redis.Redis(host="localhost", port=6379, decode_responses=True)
    try:
        await asyncio.gather(event_listener(), chronicler_loop(pub))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await pub.aclose()


if __name__ == "__main__":
    asyncio.run(main())
