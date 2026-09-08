# locutor.py — TTS microservice (narrator.broadcast -> fala local p/ OBS capturar)
# Requer: pip install edge-tts pygame-ce  (pygame-ce provê `import pygame`; precisa de saída de áudio no SO)

import asyncio
import glob
import json
import os
import uuid

import redis.asyncio as redis

VOICE = os.getenv("LOCUTOR_VOICE", "pt-BR-AntonioNeural")  # ou pt-BR-FranciscaNeural
TEMP_PREFIX = os.getenv("LOCUTOR_TEMP_PREFIX", "tts_")

queue: asyncio.Queue[str] = asyncio.Queue()  # serializa: nunca sobrepõe áudios


def _new_temp_file() -> str:
    """Arquivo único por item — nunca sobrescreve o que está tocando."""
    return f"{TEMP_PREFIX}{uuid.uuid4().hex}.mp3"


def _cleanup_stale():
    """Remove mp3s órfãos de execuções anteriores (crash no meio do play)."""
    for path in glob.glob(f"{TEMP_PREFIX}*.mp3"):
        try:
            os.remove(path)
        except OSError:
            pass


async def _speak(path: str):
    """Toca até o fim, libera o descritor e deleta — tudo sem bloquear o loop."""
    import pygame

    pygame.mixer.music.load(path)
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():  # polling async, nunca time.sleep()
        await asyncio.sleep(0.1)
    pygame.mixer.music.unload()  # libera o descritor ANTES de deletar
    os.remove(path)


async def producer():
    """Task A: assina narrator.broadcast e enfileira o texto."""
    sub = redis.Redis(host="localhost", port=6379, decode_responses=True)
    ps = sub.pubsub()
    await ps.subscribe("narrator.broadcast")
    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue
        try:
            text = json.loads(msg["data"])["text"]
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
        if text:
            await queue.put(text)


async def consumer():
    """Task B: TTS -> arquivo único -> play -> delete. Falha isolada por item."""
    import edge_tts

    while True:
        text = await queue.get()
        path = _new_temp_file()
        try:
            await edge_tts.Communicate(text, VOICE).save(path)
            await _speak(path)
        except Exception as e:  # rede/áudio falhou -> worker sobrevive
            print(f"[locutor] falhou item, pulando: {e!r}")
            try:
                if os.path.exists(path):
                    os.remove(path)  # parcial nunca fica no disco
            except OSError:
                pass
        finally:
            queue.task_done()


async def main():
    import pygame

    _cleanup_stale()
    pygame.mixer.init()
    try:
        await asyncio.gather(producer(), consumer())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        pygame.mixer.quit()  # cleanup do áudio


if __name__ == "__main__":
    asyncio.run(main())
