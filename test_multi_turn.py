"""
Test Multi-Turn Conversation Scenarios against /v1/reply:
  1. Engaged action follow-up
  2. Auto-reply handling & backoff
  3. Explicit opt-out / Stop
  4. Out-of-scope / Curveball
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

# 1. Warm up contexts
with open("dataset/categories/dentists.json") as f:
    dentists_cat = json.load(f)
with open("dataset/merchants_seed.json") as f:
    dr_meera = json.load(f)["merchants"][0]

post("/v1/context", {"scope": "category", "context_id": "dentists", "version": 1, "payload": dentists_cat})
post("/v1/context", {"scope": "merchant", "context_id": "m_001_drmeera_dentist_delhi", "version": 1, "payload": dr_meera})

print("=" * 70)
print("  MULTI-TURN TEST SUITE")
print("=" * 70)

# Scenario 1: Engaged response
print("\n[Scenario 1] Merchant says: 'Yes please send the abstract and draft the patient WhatsApp'")
status, res = post("/v1/reply", {
    "conversation_id": "conv_test_engaged_01",
    "merchant_id": "m_001_drmeera_dentist_delhi",
    "turn_number": 2,
    "message": "Yes please send the abstract and draft the patient WhatsApp"
})
print(f"Status: {status} | Action: {res.get('action')} | CTA: {res.get('cta')}")
print(f"Body: {res.get('body')}")
print(f"Rationale: {res.get('rationale')}")

# Scenario 2: Auto-reply
print("\n[Scenario 2] Merchant sends canned auto-reply")
status, res = post("/v1/reply", {
    "conversation_id": "conv_test_autoreply_01",
    "merchant_id": "m_001_drmeera_dentist_delhi",
    "turn_number": 3,
    "message": "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
})
print(f"Status: {status} | Action: {res.get('action')} | Wait Seconds: {res.get('wait_seconds')}")
print(f"Rationale: {res.get('rationale')}")

# Scenario 3: Opt-out
print("\n[Scenario 3] Merchant says: 'Not interested. Stop messaging me.'")
status, res = post("/v1/reply", {
    "conversation_id": "conv_test_optout_01",
    "merchant_id": "m_001_drmeera_dentist_delhi",
    "turn_number": 2,
    "message": "Not interested. Stop messaging me."
})
print(f"Status: {status} | Action: {res.get('action')}")
print(f"Rationale: {res.get('rationale')}")

# Scenario 4: Curveball (GST)
print("\n[Scenario 4] Merchant asks curveball: 'Can you also help me with my GST filing this month?'")
status, res = post("/v1/reply", {
    "conversation_id": "conv_test_curveball_01",
    "merchant_id": "m_001_drmeera_dentist_delhi",
    "turn_number": 2,
    "message": "Btw can you also help me with my GST filing this month?"
})
print(f"Status: {status} | Action: {res.get('action')} | CTA: {res.get('cta')}")
print(f"Body: {res.get('body')}")
print(f"Rationale: {res.get('rationale')}")
print("\n" + "=" * 70)
