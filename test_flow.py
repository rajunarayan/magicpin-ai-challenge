"""
Test script: Simulates the judge flow.
1. Push category context (dentists)
2. Push merchant context (Dr. Meera)
3. Push trigger context (research_digest)
4. Call /v1/tick -> Bot composes a message via Gemini
"""

import json
import urllib.request

BASE = "http://localhost:5000"

def post(path, data):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def get(path):
    resp = urllib.request.urlopen(f"{BASE}{path}", timeout=5)
    return resp.status, json.loads(resp.read())


# Load real data from dataset files
with open("dataset/categories/dentists.json") as f:
    dentists_category = json.load(f)

with open("dataset/merchants_seed.json") as f:
    merchants = json.load(f)["merchants"]
    dr_meera = merchants[0]  # m_001_drmeera_dentist_delhi

with open("dataset/triggers_seed.json") as f:
    triggers = json.load(f)["triggers"]
    research_trigger = triggers[0]  # trg_001_research_digest_dentists


print("=" * 70)
print("  TEST: Full Judge Flow Simulation")
print("=" * 70)

# Step 1: Push category
print("\n[1] Pushing category: dentists...")
status, resp = post("/v1/context", {
    "scope": "category",
    "context_id": "dentists",
    "version": 1,
    "payload": dentists_category
})
print(f"    Status: {status} | Accepted: {resp.get('accepted')}")

# Step 2: Push merchant
print("\n[2] Pushing merchant: Dr. Meera...")
status, resp = post("/v1/context", {
    "scope": "merchant",
    "context_id": "m_001_drmeera_dentist_delhi",
    "version": 1,
    "payload": dr_meera
})
print(f"    Status: {status} | Accepted: {resp.get('accepted')}")

# Step 3: Push trigger
print("\n[3] Pushing trigger: research_digest...")
status, resp = post("/v1/context", {
    "scope": "trigger",
    "context_id": "trg_001_research_digest_dentists",
    "version": 1,
    "payload": research_trigger
})
print(f"    Status: {status} | Accepted: {resp.get('accepted')}")

# Step 4: Verify healthz
print("\n[4] Healthz check...")
status, resp = get("/v1/healthz")
print(f"    Contexts loaded: {resp.get('contexts_loaded')}")

# Step 5: Fire tick!
print("\n[5] Firing /v1/tick (this calls Gemini - may take a few seconds)...")
status, resp = post("/v1/tick", {
    "now": "2026-04-26T10:35:00Z",
    "available_triggers": ["trg_001_research_digest_dentists"]
})

print(f"    Status: {status}")
print(f"    Actions: {len(resp.get('actions', []))}")

if resp.get("actions"):
    action = resp["actions"][0]
    print("\n" + "=" * 70)
    print("  COMPOSED MESSAGE")
    print("=" * 70)
    print(f"\n  Trigger: {action.get('trigger_id')}")
    print(f"  Send as: {action.get('send_as')}")
    print(f"  CTA:     {action.get('cta')}")
    print(f"\n  --- Message Body ---")
    print(f"  {action.get('body')}")
    print(f"\n  --- Rationale ---")
    print(f"  {action.get('rationale')}")
    print("=" * 70)
else:
    print("  [!] No actions returned. Check server logs for errors.")
