
import json
import os

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..')
keys = set()
with open(os.path.join(PROJECT_ROOT, "data", "live_eauction_data.jsonl"), "r", encoding="utf-8") as f:
    for line in f:
        try:
            data = json.loads(line)
            keys.update(data.keys())
        except: pass

print("All found keys:")
for k in sorted(list(keys)):
    print(f"- {k}")
