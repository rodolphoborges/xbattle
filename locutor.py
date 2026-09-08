# locutor.py — TTS microservice (narrator.broadcast -> fala local p/ OBS capturar)
# Requer: pip install edge-tts pygame  (pygame precisa de saída de áudio no SO)

import asyncio
import json
import os

import redis.asyncio as redis

VOICE = os.getenv("LOCUTOR_VOICE", "pt-BR-AntonioNeural")  # ou pt-BR-FranciscaNeural
TEMP_FILE = os.getenv("LOCUTOR_TEMP_FILE", "temp_audio.mp3")

queue: asyncio.Queue[str] = asyncio.Queue()  # serializa: nunca sobrepõe áudios


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
    """Task B: gera TTS, toca e só então pega o próximo da fila."""
    import edge_tts
    import pygame

    while True:
        text = await queue.get()
        try:
            await edge_tts.Communicate(text, VOICE).save(TEMP_FILE)  # gera async
            pygame.mixer.music.load(TEMP_FILE)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():  # espera terminar, sem overlap
                await asyncio.sleep(0.1)
        except Exception as e:  # rede/áudio falhou -> worker sobrevive
            print(f"[locutor] falhou item, pulando: {e!r}")
        finally:
            queue.task_done()


async def main():
    import pygame

    pygame.mixer.init()
    try:
        await asyncio.gather(producer(), consumer())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        pygame.mixer.quit()  # cleanup do áudio


if __name__ == "__main__":
    asyncio.run(main())
