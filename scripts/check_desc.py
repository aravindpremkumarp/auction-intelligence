
import json
import os

fname = os.path.join(os.path.dirname(__file__), '..', 'data', 'live_eauction_data.jsonl')
if os.path.exists(fname):
    lines = open(fname, 'r', encoding='utf-8').readlines()
    if lines:
        print(f"Total processed: {len(lines)}")
        print("\n--- First 3 Entries ---")
        for line in lines[:3]:
            item = json.loads(line)
            print(f"URL: {item.get('URL')}")
            desc = item.get('Description', 'MISSING')
            print(f"Description: {desc[:100]}...")
            print(f"Downloads: {item.get('Downloads')}")
            print("-" * 30)
    else:
        print("File empty")
else:
    print("File not found")
