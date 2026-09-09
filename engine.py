import asyncio
import json
import random
import redis.asyncio as redis

TICK_RATE = 1.0  # 1 tick/s: campanha lenta, observável a longo prazo
SPAWN_AT = 100  # ouro p/ gerar um soldado (~100s natural na Guerra)
SPAWN_AT_DUSK = 60  # Crepúsculo: tropas baratas, clímax
MARCH_TICKS = 25  # marcha abstrata: ticks até o soldado atingir o castelo
SIEGE_DMG = 5  # dano por chegada (20 golpes p/ destruir)
CASTLE_HP = 100  # vida inicial dos castelos
KINDS = ("guerreiro", "arqueiro", "mago")  # tríade pedra-papel-tesoura, sorteio puro

# --- ciclo de vida: 1h fechada em 3 eras + morte súbita (ticks = segundos) ---
ERA_HARVEST_END = 1200  # 0-20min Colheita
ERA_WAR_END = 3000  # 20-50min Guerra
ERA_DUSK_END = 3600  # 50-60min Crepúsculo
SUDDEN_BLEED_EVERY = 5  # morte súbita: 1 HP/5s nos dois
RESTART_AFTER = 30  # auto-restart 30s após a vitória
OMEN_IDLE_EVERY = 600  # sem donate por 10min -> presságio neutro

tick = 0
camp_tick = 0  # relógio da campanha atual (zera no restart)
gold = {"A": 0, "B": 0}
hp = {"A": CASTLE_HP, "B": CASTLE_HP}
march = []  # [{"faction": "A"|"B", "hits_at": int}] — chegadas agendadas
wins = {"A": 0, "B": 0}
favor = {"A": 0, "B": 0}  # placar de devoção: prova que mais donates = vence
game_over = None  # None | "A" | "B" (vencedor); congela e agenda restart
restart_at = 0
era = "colheita"
rally_until = {"A": 0, "B": 0}  # ouro dobrado até o tick
freeze_until = {"A": 0, "B": 0}  # ouro congelado até o tick (maldição)
eclipse_until = 0  # dano dobrado p/ os dois até o tick
streak_by, streak_n, bonus_next = None, 0, {}  # momentum: 3 hits seguidos
last_donate_tick = 0


def _enemy(faction: str) -> str:
    return "B" if faction == "A" else "A"


def _spawn_at() -> int:
    return SPAWN_AT_DUSK if era == "crepusculo" else SPAWN_AT


def _state() -> dict:
    return {"tick": tick, "camp_tick": camp_tick, "era": era,
            "base_A_gold": gold["A"], "base_B_gold": gold["B"],
            "base_A_hp": hp["A"], "base_B_hp": hp["B"],
            "march_A": sum(1 for m in march if m["faction"] == "A"),
            "march_B": sum(1 for m in march if m["faction"] == "B"),
            "favor_A": favor["A"], "favor_B": favor["B"],
            "wins_A": wins["A"], "wins_B": wins["B"], "game_over": game_over,
            "restart_in": max(0, restart_at - tick) if game_over else 0}


def _era_for(ct: int) -> str:
    if ct < ERA_HARVEST_END:
        return "colheita"
    if ct < ERA_WAR_END:
        return "guerra"
    return "crepusculo"


async def _spawn(pub: redis.Redis, faction: str):
    """Forja um soldado: agenda a marcha e anuncia a classe sorteada."""
    kind = random.choice(KINDS)
    march.append({"faction": faction, "hits_at": tick + MARCH_TICKS})
    await pub.publish("game.events", json.dumps(
        {"tick": tick, "event_msg": f"Faction {faction} Spawn Soldier", "kind": kind}))


async def _hit(pub: redis.Redis, faction: str):
    """Chegada: dano real (eclipse dobra, momentum bonifica a 3ª seguida)."""
    global streak_by, streak_n, game_over
    foe = _enemy(faction)
    dmg = SIEGE_DMG * (2 if tick <= eclipse_until else 1)
    if streak_by == faction:
        streak_n += 1
    else:
        streak_by, streak_n = faction, 1
    if streak_n >= 3:
        dmg += 3
        streak_by, streak_n = None, 0
        await pub.publish("game.events", json.dumps(
            {"tick": tick, "event_msg": f"🔥 MOMENTUM {faction}! próximo golpe esmaga!"}))
    hp[foe] = max(0, hp[foe] - dmg)
    if hp[foe] <= 0:
        game_over = faction
        wins[faction] += 1
        await pub.publish("game.events", json.dumps(
            {"tick": tick, "event_msg": f"🏆 {faction} DESTROYS/DESTRÓI o castelo {foe} — {faction} WINS/VENCE!"}))
    else:
        await pub.publish("game.events", json.dumps(
            {"tick": tick, "event_msg": f"⚔ {faction} atinge/hits o castelo {foe}! (HP {hp[foe]})"}))


async def _resolve_march(pub: redis.Redis):
    for m in [x for x in march if x["hits_at"] <= tick]:
        march.remove(m)
        await _hit(pub, m["faction"])
        if game_over:
            return


async def _restart(pub: redis.Redis, why: str):
    """Nova campanha: zera relógio, ouro, HP, marchas e efeitos."""
    global game_over, camp_tick, streak_by, streak_n, era, last_donate_tick
    global eclipse_until, restart_at
    game_over, camp_tick = None, 0
    era = "colheita"
    streak_by, streak_n = None, 0
    eclipse_until = 0
    restart_at = 0
    bonus_next.clear()
    last_donate_tick = tick
    for faction in ("A", "B"):
        gold[faction] = 0
        hp[faction] = CASTLE_HP
        rally_until[faction] = 0
        freeze_until[faction] = 0
    march.clear()
    await pub.publish("game.events", json.dumps(
        {"tick": tick, "event_msg": f"🚩 Nova campanha iniciada / New campaign started ({why})"}))


async def _omen(pub: redis.Redis, faction: str | None):
    """Presságio: eclipse (dano ×2), névoa (marchas +10) ou colheita (+50 ouro)."""
    global eclipse_until
    roll = random.choice(["eclipse", "nevoa", "colheita"])
    if roll == "eclipse":
        eclipse_until = tick + 60
        msg = "🌑 ECLIPSE! dano dobrado por 60s!"
    elif roll == "nevoa":
        for m in march:
            m["hits_at"] += 10
        msg = "🌫 NÉVOA! marchas atrasadas!"
    else:
        gold["A"] += 50
        gold["B"] += 50
        msg = "🌾 COLHEITA! +50 ouro p/ todos!"
    if faction:
        favor[faction] += 5
    await pub.publish("game.events", json.dumps({"tick": tick, "event_msg": msg}))


async def _do_action(pub: redis.Redis, action: str, target: str, value: int):
    """As 6 ações do taverneiro. Donate pós-vitória = restart imediato."""
    global last_donate_tick
    if game_over is not None:
        await _restart(pub, "donate pós-vitória")
        return
    last_donate_tick = tick
    if action == "donate":
        if value <= 0:
            return  # ignora donate zerado/negativo: sem drenar ouro nem farmar favor
        gold[target] += value
        favor[target] += max(1, value // 25)
        await pub.publish("game.events", json.dumps(
            {"tick": tick, "event_msg": f"🪙 Donate +{value} to {target}"}))
    elif action == "recruit":
        favor[target] += 4
        await _spawn(pub, target)
        await pub.publish("game.events", json.dumps(
            {"tick": tick, "event_msg": f"📯 {target} recruta na hora! (recruit)"}))
    elif action == "heal":
        favor[target] += 3
        hp[target] = min(CASTLE_HP, hp[target] + 10)
        await pub.publish("game.events", json.dumps(
            {"tick": tick, "event_msg": f"💚 {target} cura o castelo! (HP {hp[target]})"}))
    elif action == "rally":
        favor[target] += 3
        rally_until[target] = tick + 60
        await pub.publish("game.events", json.dumps(
            {"tick": tick, "event_msg": f"📯 {target} em RALLY! ouro dobrado 60s!"}))
    elif action == "curse":
        favor[target] += 3
        freeze_until[_enemy(target)] = tick + 30
        await pub.publish("game.events", json.dumps(
            {"tick": tick, "event_msg": f"🌑 {target} amaldiçoa {_enemy(target)}! ouro congelado 30s!"}))
    elif action == "omen":
        await _omen(pub, target)


async def tick_loop(pub: redis.Redis):
    """TASK 1: Tick Loop — eras, ouro, spawns, cerco, morte súbita, restart."""
    global tick, camp_tick, era, game_over, restart_at
    while True:
        tick += 1
        if game_over is not None:
            if tick >= restart_at:  # loop autônomo: vitória -> nova era
                await _restart(pub, "ciclo de 1h")
        else:
            camp_tick += 1
            new_era = _era_for(camp_tick)
            if new_era != era:  # transição de era = evento crítico
                era = new_era
                names = {"colheita": "🌅 ERA DA COLHEITA", "guerra": "⚔ ERA DA GUERRA", "crepusculo": "🌇 ERA DO CREPÚSCULO"}
                await pub.publish("game.events", json.dumps(
                    {"tick": tick, "event_msg": f"{names[era]}!"}))
            if camp_tick > ERA_DUSK_END and (tick % SUDDEN_BLEED_EVERY == 0):
                for faction in ("A", "B"):  # morte súbita: sangra os dois
                    hp[faction] = max(0, hp[faction] - 1)
                await pub.publish("game.events", json.dumps(
                    {"tick": tick, "event_msg": f"☠ MORTE SÚBITA! HP A:{hp['A']} B:{hp['B']}"}))
                dead = [f for f in ("A", "B") if hp[f] <= 0]
                if dead:
                    if len(dead) == 2:  # empate: devoção decide, sorte desempata
                        if favor["A"] != favor["B"]:
                            game_over = "A" if favor["A"] > favor["B"] else "B"
                        else:
                            game_over = random.choice(["A", "B"])
                    else:
                        game_over = _enemy(dead[0])
                    wins[game_over] += 1
                    restart_at = tick + RESTART_AFTER
                    await pub.publish("game.events", json.dumps(
                        {"tick": tick, "event_msg": f"🏆 {game_over} VENCE na morte súbita!"}))
            for faction in ("A", "B"):
                if tick <= freeze_until[faction]:
                    continue  # maldição: ouro congelado
                gold[faction] += 2 if tick <= rally_until[faction] else 1
                if gold[faction] >= _spawn_at():
                    gold[faction] = 0
                    await _spawn(pub, faction)
            await _resolve_march(pub)
            if game_over is not None:
                restart_at = tick + RESTART_AFTER
            if tick - last_donate_tick > OMEN_IDLE_EVERY and game_over is None:
                await _omen(pub, None)  # hora quieta: os deuses intervêm
        await pub.publish("game.state", json.dumps(_state()))
        await asyncio.sleep(TICK_RATE)


async def cmd_listener(pub: redis.Redis):
    """TASK 2: PubSub Listener — sole reader of cmd.interact (auto-reconnect)."""
    while True:
        sub = None
        try:
            sub = redis.Redis(host="localhost", port=6379, decode_responses=True)
            ps = sub.pubsub()
            await ps.subscribe("cmd.interact")
            async for msg in ps.listen():
                await _handle_cmd(pub, msg)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as e:  # Redis caiu: espera e reassina, nunca morre
            print(f"[engine] cmd listener reconnecting: {e!r}")
            await asyncio.sleep(2)
        finally:
            if sub is not None:
                try:
                    await sub.aclose()
                except Exception:
                    pass


async def _handle_cmd(pub: redis.Redis, msg: dict):
    if msg.get("type") != "message":
        return
    try:
        data = json.loads(msg["data"])  # {"action","target","value"}
    except (json.JSONDecodeError, TypeError):
        return
    action = str(data.get("action", "donate")).lower()
    target = str(data.get("target", "")).upper()
    if action not in ("donate", "recruit", "heal", "rally", "curse", "omen"):
        return
    if target not in gold:
        return
    try:
        value = int(data.get("value", 0))
    except (ValueError, TypeError):
        return
    await _do_action(pub, action, target, value)


async def main():
    pub = redis.Redis(host="localhost", port=6379, decode_responses=True)
    try:
        await asyncio.gather(tick_loop(pub), cmd_listener(pub))  # segregation
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await pub.aclose()


if __name__ == "__main__":
    asyncio.run(main())
