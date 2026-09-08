import asyncio
import json
import redis.asyncio as redis

TICK_RATE = 1.0  # 1 tick/s: campanha lenta, observável a longo prazo
SPAWN_AT = 100  # ouro p/ gerar um soldado (~100s natural por facção)
tick = 0
gold = {"A": 0, "B": 0}


async def tick_loop(pub: redis.Redis):
    """TASK 1: Tick Loop — sole writer of tick/gold on timer."""
    global tick
    while True:
        tick += 1
        for faction in ("A", "B"):
            gold[faction] += 1  # +1 gold per tick
            if gold[faction] >= SPAWN_AT:  # limiar de spawn
                gold[faction] = 0  # reset after spawn
                await pub.publish("game.events", json.dumps(
                    {"tick": tick, "event_msg": f"Faction {faction} Spawn Soldier"}))
        await pub.publish("game.state", json.dumps(  # state every tick
            {"tick": tick, "base_A_gold": gold["A"], "base_B_gold": gold["B"]}))
        await asyncio.sleep(TICK_RATE)  # non-blocking cadence


async def cmd_listener(pub: redis.Redis):
    """TASK 2: PubSub Listener — sole reader of cmd.interact."""
    sub = redis.Redis(host="localhost", port=6379, decode_responses=True)
    ps = sub.pubsub()
    await ps.subscribe("cmd.interact")
    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue
        try:
            data = json.loads(msg["data"])  # {"action","target","value"}
        except (json.JSONDecodeError, TypeError):
            continue
        if data.get("action") == "donate" and data.get("target") in gold:
            try:
                gold[data["target"]] += int(data.get("value", 0))
            except (ValueError, TypeError):
                continue
            await pub.publish("game.events", json.dumps(  # instant feedback
                {"tick": tick, "event_msg": f"Donate +{data['value']} to {data['target']}"}))


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
