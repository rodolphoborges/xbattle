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

SYSTEM_PROMPT = "Você é um narrador frenético de eSports transmitindo uma guerra medieval ao vivo. Regras absolutas: 1) Responda APENAS com a narração — sem introdução, confirmação ou formalidade. 2) No máximo 3 frases curtas, altamente dramáticas, baseadas SOMENTE nos eventos recebidos. 3) Sem emojis, hashtags ou explicação do seu raciocínio."

FALLBACK_TEXT = "Os idiotas continuaram se matando e nem isso fizeram direito."

event_buffer = []  # sliding window of game.events
flush_signal = asyncio.Event()  # listener -> loop: flush sem bloquear o Redis


def _threshold() -> int:
    try:
        return int(os.getenv("CRONISTA_THRESHOLD", "5"))
    except (ValueError, TypeError):
        return 5


def _max_silence() -> float:
    try:
        return float(os.getenv("CRONISTA_MAX_SILENCE", "10"))
    except (ValueError, TypeError):
        return 10.0


def _is_critical(evt: dict) -> bool:
    """Spawn = narração imediata; resto acumula até threshold ou silêncio."""
    return "spawn" in str(evt.get("event_msg", "")).lower()


def _client() -> tuple[AsyncOpenAI, str]:
    """Build AsyncOpenAI from .env (works for Ollama LOCAL and CLOUD)."""
    base_url = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    api_key = os.getenv("LLM_API_KEY", "ollama")  # Ollama ignores key
    model = os.getenv("LLM_MODEL_NAME", "llama3:8b")
    return AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=25.0), model


async def generate_commentary(events: list) -> str:
    """Task C: call LLM with flushed events, return narration text."""
    client, model = _client()
    # Reasoning models (e.g. Gemma via LM Studio) spend a variable
    # 300-700+ tokens thinking; a tight budget truncates the answer
    # (finish_reason=length, empty content). Retry = independent sample.
    try:
        max_tokens = int(os.getenv("LLM_MAX_TOKENS", "1200"))
    except (ValueError, TypeError):
        max_tokens = 1200
    user_prompt = f"Relatórios de batalha:\n{json.dumps(events, ensure_ascii=False)}"
    for _ in range(2):
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.9,
            max_tokens=max_tokens,
        )
        text = (resp.choices[0].message.content or "").strip()
        if text:
            return text
    return ""


async def event_listener():
    """Task A: subscribe game.events, append; sinaliza flush sem bloquear."""
    sub = redis.Redis(host="localhost", port=6379, decode_responses=True)
    ps = sub.pubsub()
    await ps.subscribe("game.events")
    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue
        try:
            evt = json.loads(msg["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        event_buffer.append(evt)
        if _is_critical(evt) or len(event_buffer) >= _threshold():
            flush_signal.set()  # threshold atingido -> flush imediato


async def chronicler_loop(pub: redis.Redis):
    """Task B: flush imediato no gatilho; fallback após MAX_SILENCE de silêncio."""
    while True:
        try:
            await asyncio.wait_for(flush_signal.wait(), timeout=_max_silence())
        except (asyncio.TimeoutError, TimeoutError):
            pass  # silêncio: descarrega o que acumulou (ou pula se vazio)
        if not event_buffer:
            flush_signal.clear()
            continue
        events = list(event_buffer)
        event_buffer.clear()  # bloco síncrono: sem race com o listener
        flush_signal.clear()  # rearma o timer p/ o próximo ciclo
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
