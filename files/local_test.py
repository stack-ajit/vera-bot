#!/usr/bin/env python3
"""
local_test.py — run a quick self-test of the bot without the judge harness.

Usage:
    # Start the bot first:  uvicorn bot:app --port 8080
    # Then in another terminal:
    python local_test.py

Loads all seed JSONs, pushes them to /v1/context, runs a tick, and plays
one round of simulated merchant replies per action. Prints a summary.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    import subprocess; subprocess.run([sys.executable, "-m", "pip", "install", "httpx", "--break-system-packages", "-q"])
    import httpx

BOT_URL = "http://localhost:8080"
SEED_DIR = Path(__file__).parent

# Sample merchant replies for interactive testing
MERCHANT_REPLIES = {
    "accept": "Yes please, send it",
    "question": "What does this mean for my clinic?",
    "reject": "No thanks, not interested right now",
    "join": "Main magicpin se judna chahta hoon",
}


def push(client: httpx.Client, scope: str, context_id: str, payload: dict, version: int = 1):
    r = client.post(f"{BOT_URL}/v1/context", json={
        "scope": scope, "context_id": context_id, "version": version,
        "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"
    }, timeout=30)
    r.raise_for_status()
    data = r.json()
    status = "✓" if data.get("accepted") else f"✗ ({data.get('reason')})"
    print(f"  [{status}] {scope}/{context_id} v{version}")
    return data


def main():
    print("=" * 60)
    print("Vera Bot — Local Self-Test")
    print("=" * 60)

    with httpx.Client() as c:
        # 1. Health check
        print("\n[1] Health check…")
        r = c.get(f"{BOT_URL}/v1/healthz", timeout=5)
        print(f"  Status: {r.json()}")

        # 2. Push category contexts
        print("\n[2] Pushing category contexts…")
        categories_dir = SEED_DIR.parent  # categories are at the seed level
        for cat_file in ["dentists.json", "gyms.json", "pharmacies.json", "restaurants.json", "salons.json"]:
            cat_path = Path("/mnt/user-data/uploads") / cat_file
            if cat_path.exists():
                with open(cat_path) as f:
                    cat = json.load(f)
                push(c, "category", cat["slug"], cat)

        # 3. Push merchant seeds
        print("\n[3] Pushing merchant contexts…")
        merchants_path = Path("/mnt/user-data/uploads/merchants_seed.json")
        if merchants_path.exists():
            with open(merchants_path) as f:
                merchants = json.load(f)["merchants"]
            for m in merchants:
                push(c, "merchant", m["merchant_id"], m)

        # 4. Push customer seeds
        print("\n[4] Pushing customer contexts…")
        customers_path = Path("/mnt/user-data/uploads/customers_seed.json")
        if customers_path.exists():
            with open(customers_path) as f:
                customers = json.load(f)["customers"]
            for cust in customers:
                push(c, "customer", cust["customer_id"], cust)

        # 5. Push trigger seeds
        print("\n[5] Pushing trigger contexts…")
        triggers_path = Path("/mnt/user-data/uploads/triggers_seed.json")
        if triggers_path.exists():
            with open(triggers_path) as f:
                triggers = json.load(f)["triggers"]
            for trg in triggers:
                push(c, "trigger", trg["id"], trg)

        # 6. Health check again (should show contexts loaded)
        print("\n[6] Health check after loads…")
        r = c.get(f"{BOT_URL}/v1/healthz", timeout=5)
        print(f"  {r.json()}")

        # 7. Tick — pick first 5 triggers
        print("\n[7] Running /v1/tick with first 5 triggers…")
        trigger_ids = [t["id"] for t in triggers[:5]]
        r = c.post(f"{BOT_URL}/v1/tick", json={
            "now": "2026-04-26T10:30:00Z",
            "available_triggers": trigger_ids,
        }, timeout=60)
        r.raise_for_status()
        tick_data = r.json()
        actions = tick_data.get("actions", [])
        print(f"  Got {len(actions)} action(s)\n")

        for i, action in enumerate(actions, 1):
            print(f"  ── Action {i}: {action['conversation_id']} ──")
            print(f"     trigger: {action['trigger_id']}")
            print(f"     send_as: {action['send_as']}")
            print(f"     body: {action['body'][:150]}{'…' if len(action['body'])>150 else ''}")
            print(f"     cta: {action['cta']}")
            print(f"     rationale: {action.get('rationale','')[:100]}")
            print()

        # 8. Simulate a merchant reply to the first action
        if actions:
            action = actions[0]
            print("\n[8] Simulating merchant reply (accept) to first action…")
            r = c.post(f"{BOT_URL}/v1/reply", json={
                "conversation_id": action["conversation_id"],
                "merchant_id": action["merchant_id"],
                "customer_id": action.get("customer_id"),
                "from_role": "merchant",
                "message": MERCHANT_REPLIES["accept"],
                "received_at": "2026-04-26T10:35:00Z",
                "turn_number": 2,
            }, timeout=30)
            r.raise_for_status()
            reply_data = r.json()
            print(f"  action: {reply_data['action']}")
            if reply_data.get("body"):
                print(f"  body: {reply_data['body'][:200]}")
            print(f"  rationale: {reply_data.get('rationale','')}")

            # 9. Simulate a reject
            print("\n[9] Simulating merchant reject…")
            r = c.post(f"{BOT_URL}/v1/reply", json={
                "conversation_id": action["conversation_id"],
                "merchant_id": action["merchant_id"],
                "customer_id": action.get("customer_id"),
                "from_role": "merchant",
                "message": MERCHANT_REPLIES["reject"],
                "received_at": "2026-04-26T10:40:00Z",
                "turn_number": 3,
            }, timeout=30)
            r.raise_for_status()
            reject_data = r.json()
            print(f"  action: {reject_data['action']}  (expected: end or wait)")
            print(f"  rationale: {reject_data.get('rationale','')}")

        # 10. Second tick — should NOT re-send suppressed triggers
        print("\n[10] Running second /v1/tick (should show suppression)…")
        r = c.post(f"{BOT_URL}/v1/tick", json={
            "now": "2026-04-26T10:35:00Z",
            "available_triggers": trigger_ids,
        }, timeout=60)
        r.raise_for_status()
        tick2 = r.json()
        print(f"  Got {len(tick2.get('actions', []))} action(s) (suppressed triggers should be 0 or fewer)")

    print("\n" + "=" * 60)
    print("Self-test complete.")


if __name__ == "__main__":
    main()
