import json
import redis

r = redis.Redis(host="localhost", port=6379, decode_responses=True)

print('Donate format: "<TARGET> <VALUE>" e.g. "A 50" | q to quit')
while True:
    s = input("donate> ").strip()  # blocking CLI is fine here
    if s.lower() in ("q", "quit", "exit"):
        break
    try:
        target, raw = s.split()  # "A 50" -> Target A, Value 50
        target, value = target.upper(), int(raw)
        assert target in ("A", "B")
    except (ValueError, AssertionError):
        print('Invalid. Use e.g. "A 50" or "B 20"')
        continue
    r.publish("cmd.interact", json.dumps(  # contract payload
        {"action": "donate", "target": target, "value": value}))
    print(f"Sent donate +{value} to {target}")
