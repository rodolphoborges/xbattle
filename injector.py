import json
import redis

ACTIONS = ("donate", "recruit", "heal", "rally", "curse", "omen")

r = redis.Redis(host="localhost", port=6379, decode_responses=True)

print('Format: "<TARGET> <VALUE>" e.g. "A 50" (= donate) | "<ACTION> <TARGET> [VALUE]" e.g. "recruit A" | q to quit')
print(f"Actions: {', '.join(ACTIONS)}")
while True:
    s = input("donate> ").strip()  # blocking CLI is fine here
    if s.lower() in ("q", "quit", "exit"):
        break
    parts = s.split()
    try:
        if len(parts) == 2 and parts[0].upper() in ("A", "B"):
            # legado: "A 50" -> donate A 50
            action, target, value = "donate", parts[0].upper(), int(parts[1])
        elif len(parts) in (2, 3) and parts[0].lower() in ACTIONS:
            action, target = parts[0].lower(), parts[1].upper()
            assert target in ("A", "B")
            value = int(parts[2]) if len(parts) == 3 else 0
        else:
            raise ValueError
        assert target in ("A", "B")
        if action == "donate" and value <= 0:
            raise ValueError
    except (ValueError, AssertionError):
        print('Invalid. Use "A 50", "B 20" or e.g. "recruit A", "heal B", "rally A", "curse A", "omen A"')
        continue
    r.publish("cmd.interact", json.dumps(  # contract payload
        {"action": action, "target": target, "value": value}))
    extra = f" +{value}" if action == "donate" else ""
    print(f"Sent {action}{extra} to {target}")
