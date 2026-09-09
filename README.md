# xbattle — guerra autônoma zero-player (PoC EDA)

Batalha A vs B 100% dirigida a eventos via **Redis Pub/Sub + JSON**.
O `engine.py` é o único escritor do estado; `cronista.py` narra via LLM;
`gateway.py` espelha tudo no viewer; `locutor.py` fala a narração p/ captura no OBS;
`injector.py` é o “taverneiro” (intervenção humana).

## Quickstart

```bash
pip install -r requirements.txt
# Redis em localhost:6379 (sem Docker neste repo)
python engine.py        # terminal 1
python cronista.py      # terminal 2
python injector.py      # terminal 3 — "A 50" (donate) ou "recruit A", "heal B", "rally A", "curse A", "omen A"
uvicorn gateway:app --port 8000  # terminal 4 — abra http://localhost:8000
python locutor.py       # terminal 5 (opcional, precisa saída de áudio)
```

`.env` (gitignored, nunca commitar):

```ini
LLM_PROVIDER=LOCAL
LLM_BASE_URL=http://localhost:1234/v1
LLM_API_KEY=lm-studio
LLM_MODEL_NAME=google/gemma-4-12b
LLM_MAX_TOKENS=1200
CRONISTA_THRESHOLD=5
CRONISTA_MAX_SILENCE=10
LOCUTOR_VOICE=pt-BR-AntonioNeural
LOCUTOR_TEMP_PREFIX=tts_
```

`LLM_PROVIDER` é só etiqueta informativa (LOCAL|CLOUD) — o código não a lê;
`_client()` usa apenas `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL_NAME`.

## Arquitetura

```
injector --cmd.interact--> engine --game.state/game.events--> gateway --ws--> index.html (read-only)
                              engine --game.events--> cronista --narrator.broadcast--> gateway/locutor
```

Canais (sempre JSON, `decode_responses=True`):

| Canal | Produtor | Consumidor | Payload |
|---|---|---|---|
| `cmd.interact` | injector | engine | `{"action":"donate\|recruit\|heal\|rally\|curse\|omen","target":"A\|B","value":int}` |
| `game.state` | engine (1 tps) | gateway/viewer | `tick, camp_tick, era, base_A/B_gold, base_A/B_hp, march_A/B, favor_A/B, wins_A/B, game_over, restart_in` |
| `game.events` | engine | cronista/gateway | `{"tick","event_msg","kind"?}` |
| `narrator.broadcast` | cronista | gateway/locutor | `{"text","timestamp":int}` |

Regras de robustez: ignorar acks (`type != "message"`), `try/except JSONDecodeError`,
`aclose()` no `finally` em todo listener com auto-reconnect, nunca crashear no LLM/TTS.

## Engine — regras do jogo

- Tick `TICK_RATE=1.0s`. Ouro +1/tick (+2 em `rally`, 0 em `freeze`).
- Spawn quando ouro >= 100 (60 no `crepusculo`), zera o ouro, agenda marcha `MARCH_TICKS=25`.
- Chegada = `SIEGE_DMG=5` no HP inimigo (`CASTLE_HP=100`). 20 golpes destroem.
- Eras (ticks = segundos): `colheita` 0–1200, `guerra` 1200–3000, `crepusculo` 3000–3600.
- Morte súbita pós-crepúsculo: -1 HP/5s nos dois. Empate (ambos zeram juntos) = maior `favor` vence, sorteio se igual.
- Momentum: 3 hits seguidos do mesmo lado = +3 dano + evento `MOMENTUM`.
- `wins`/`favor` persistem entre campanhas. Auto-restart 30s após vitória; `donate` pós-vitória reinicia na hora.
- `_restart` zera relógio, ouro, HP, marchas, `rally/freeze/eclipse`, streak.
- Omen: idle 600s sem donate (ou ação `omen`) sorteia `eclipse` (dano ×2 60s) / `nevoa` (+10 ticks nas marchas) / `colheita` (+50 ouro p/ todos).
- `donate value<=0` é ignorado (sem drenar ouro nem farmar favor).

Ações do taverneiro:

| Ação | Efeito | Favor |
|---|---|---|
| `donate A 50` | +50 ouro p/ A | `max(1, value//25)` |
| `recruit A` | spawn imediato p/ A | +4 |
| `heal A` | +10 HP (teto 100) | +3 |
| `rally A` | ouro dobrado 60s | +3 |
| `curse A` | congela ouro de B 30s | +3 |
| `omen A` | presságio aleatório | +5 (só no omen manual; o omen idle automático não dá favor) |

## Entrypoints

- `engine.py`: `tick_loop(pub)` + `cmd_listener(pub)` via `asyncio.gather`. `pub` passado como arg.
- `injector.py`: CLI sync (sem asyncio). Aceita legado `"A 50"` (= donate) e `"<ACTION> <TARGET> [VALUE]"`.
- `cronista.py`: `event_listener()` acumula em `event_buffer`, dispara `flush_signal` em evento crítico ou `len >= CRONISTA_THRESHOLD` (5); `chronicler_loop()` faz flush imediato ou após `CRONISTA_MAX_SILENCE` (10s), pula se vazio, publica fallback em erro.
- `gateway.py`: `redis_pump()` único → broadcast WS envelopado. `GET /` serve `index.html`, `/ws` só empurra (cliente nunca envia).
- `locutor.py`: fila `asyncio.Queue` produtor→consumidor serial. Voz/prefixo lidos do env a cada item (`LOCUTOR_VOICE`, `LOCUTOR_TEMP_PREFIX`). Arquivo `tts_<uuid>.mp3` único, `unload()` antes de deletar.
- `index.html`: canvas pixel-art (bg offscreen, 3 lanes, castelos, tropas com nome/classe, duelos, projéteis, clima cosmético, festa vitória). HP/marcha/vitória vêm do backend. WS via `location.host` (ws/wss automático). `window.xbattle` exposto p/ debug/teste.

## LLM / narração

`SYSTEM_PROMPT` (pt-BR, frenético eSports, max 3 frases, sem raciocínio exposto) é **spec-frozen — não reformular**.
`FALLBACK_TEXT` nunca vazio, sem emojis/hashtags.
`AsyncOpenAI(timeout=25.0, temperature=0.9, max_tokens=LLM_MAX_TOKENS default 1200)` + 1 retry em resposta vazia
(modelos reasoning tipo Gemma gastam 300–700 tokens pensando; budget curto retorna `finish_reason=length` vazio).

## Verificação

```bash
python -m py_compile *.py
# testes manuais: _check_siege.py (spawn→marcha→dano→vitória→restart), _check_names.py (viewer via playwright)
# após rodar: apagar __pycache__/ e _check_*.py temporários (já gitignored)
```

Troubleshooting: sem Redis → todos os listeners logam `reconnecting` e tentam a cada 2s;
sem LLM → cronista publica fallback; sem áudio → locutor pula o item e segue.
Logs em `logs/` (gitignored).
