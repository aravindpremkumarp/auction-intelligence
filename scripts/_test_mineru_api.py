"""Smoke test: confirm MinerU API auth + endpoint work with one image."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ.get("MINERU_API_KEY")
if not KEY:
    sys.exit("MINERU_API_KEY not set in environment")

BASE = "https://mineru.net/api/v4"
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

# Pick 1 known-bad case (663028 SFC tabular notice)
TEST_FILE = Path("downloads/tn_properties/8a0a9925-c2a9-4cb1-887c-6905cd0b24bf17678677694981.jpg")
assert TEST_FILE.exists(), f"missing: {TEST_FILE}"

# Step 1: request batch upload URL
print("Step 1: POST /file-urls/batch")
r = requests.post(
    f"{BASE}/file-urls/batch",
    headers=HEADERS,
    json={"files": [{"name": TEST_FILE.name, "data_id": "test_663028"}],
          "model_version": "vlm"},
    timeout=30,
)
print(f"  HTTP {r.status_code}")
print(f"  body: {r.text[:600]}")
r.raise_for_status()
data = r.json().get("data", {})
batch_id = data.get("batch_id")
file_urls = data.get("file_urls", [])
print(f"  batch_id: {batch_id}")
print(f"  file_urls[0]: {file_urls[0][:80] if file_urls else 'NONE'}...")

# Step 2: PUT the file
print("\nStep 2: PUT file to signed URL")
with open(TEST_FILE, "rb") as f:
    put_resp = requests.put(file_urls[0], data=f.read(), timeout=120)
print(f"  HTTP {put_resp.status_code}")

# Step 3: poll
print("\nStep 3: poll /extract-results/batch/{batch_id}")
poll_url = f"{BASE}/extract-results/batch/{batch_id}"
for i in range(60):  # up to 5 min
    pr = requests.get(poll_url, headers=HEADERS, timeout=30)
    body = pr.json()
    print(f"  [{i:>2}] HTTP {pr.status_code}  body[:300]={str(body)[:300]}")
    if pr.status_code != 200:
        break
    rows = body.get("data", {}).get("extract_result") or body.get("data", {}).get("results") or []
    if rows:
        states = [r.get("state") for r in rows]
        if all(s in ("done", "failed") for s in states):
            print(f"\nDONE. Per-file states: {states}")
            for r in rows:
                print(f"  state={r.get('state')}  full_zip_url={r.get('full_zip_url')}")
                if r.get("err_msg"):
                    print(f"  err_msg: {r['err_msg']}")
            break
    time.sleep(5)
