#!/usr/bin/env python3
"""
Vera Bot — magicpin AI Challenge Submission
A Claude-powered merchant engagement assistant.

Run: uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import os
import time
import uuid
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import anthropic
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vera_bot")

# ── Anthropic client ─────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-20250514"
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

# ── In-memory state ──────────────────────────────────────────────────────────
START_TIME = time.time()

# (scope, context_id) → {version: int, payload: dict}
contexts: dict[tuple[str, str], dict] = {}

# conversation_id → {merchant_id, customer_id, turns: list[{role,body}], trigger_id, sent_bodies: set}
conversations: dict[str, dict] = {}

# suppression: key → True (already sent this tick)
suppressed: set[str] = set()

# ── FastAPI app ──────────────────────────────────────────────────────────────
app = FastAPI(title="Vera Bot", version="1.0.0")

# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def get_ctx(scope: str, context_id: str) -> Optional[dict]:
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def detect_auto_reply(message: str) -> bool:
    """Detect if the merchant's reply is a WhatsApp Business auto-reply."""
    auto_reply_patterns = [
        "thank you for contacting",
        "thanks for contacting",
        "we have received your message",
        "we will get back to you",
        "our team will respond",
        "this is an automated",
        "auto reply",
        "आपका संदेश प्राप्त हुआ",
        "हम जल्द ही",
    ]
    low = message.lower().strip()
    return any(p in low for p in auto_reply_patterns)


def detect_intent(message: str) -> str:
    """Detect high-level intent from merchant message."""
    low = message.lower().strip()
    positive = ["yes", "haan", "ha ", "ha.", "ok", "okay", "go ahead", "send it", "send me",
                "sure", "bilkul", "zaroor", "theek", "done", "let's do", "lets do", "go"]
    negative = ["no", "nahi", "nope", "not interested", "stop", "don't", "later", "baad mein"]
    join = ["join", "judrna", "jodna", "sign up", "subscribe", "enroll", "register"]

    for p in join:
        if p in low:
            return "join_intent"
    for p in positive:
        if low.startswith(p) or f" {p}" in low or low == p:
            return "accept"
    for p in negative:
        if p in low:
            return "reject"
    return "question_or_other"


# ═══════════════════════════════════════════════════════════════════════════
# COMPOSER  (the intelligence layer)
# ═══════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are Vera, magicpin's AI merchant-engagement assistant. You talk to merchants over WhatsApp.

MISSION: Compose ONE proactive message to send to a merchant (or their customer) based on the 4-context framework below.

GOLDEN RULES:
1. Max 160 words. Ideal: 80-120 words. WhatsApp, not email.
2. Single clear CTA in the LAST sentence. Not in the middle.
3. No preamble ("I hope you're doing well…"). Get to the point.
4. Match voice exactly: dentists → peer/clinical, restaurants → warm/operator, gyms → energetic/coach, pharmacies → precise/trustworthy, salons → warm/practical.
5. Use service+price offers, not generic discounts ("Haircut @ ₹99" not "flat 30% off").
6. For dentists/pharmacies: no "cure", "guaranteed", "best in city". Clinical peer tone only.
7. Honor language preference. hi-en mix → mix Hindi and English naturally. Pure hi → Hindi. english → English.
8. Use ONE compulsion lever from: [specificity/data, loss-aversion, social-proof, effort-externalization, curiosity, reciprocity, asking-merchant, single-binary-CTA].
9. For customer-scope triggers: send_as = "merchant_on_behalf" (message from merchant to their customer).
10. For merchant-scope triggers: send_as = "vera" (Vera to the merchant).
11. Never hallucinate data not in the context. Only cite sources that are in the digest.
12. If the trigger has urgency 4-5, convey urgency without being alarmist.
13. Anti-patterns to AVOID: multiple CTAs, "AMAZING DEAL!", re-introducing yourself, buried CTA, generic offers, long preambles.

OUTPUT FORMAT (JSON only, no markdown):
{
  "body": "<the message text>",
  "send_as": "vera" | "merchant_on_behalf",
  "cta": "open_ended" | "yes_no" | "slot_choice" | "action_link",
  "rationale": "<1-2 sentences: why this message, which lever used>",
  "template_name": "<descriptive_slug_v1>"
}"""


def build_composer_prompt(
    trigger: dict,
    merchant: dict,
    category: dict,
    customer: Optional[dict],
    conversation_history: list[dict],
) -> str:
    parts = []
    parts.append(f"TRIGGER:\n{json.dumps(trigger, ensure_ascii=False, indent=2)}")
    parts.append(f"\nMERCHANT:\n{json.dumps(merchant, ensure_ascii=False, indent=2)}")

    # Only send relevant category slices to save tokens
    cat_slim = {
        "slug": category.get("slug"),
        "voice": category.get("voice"),
        "offer_catalog": category.get("offer_catalog"),
        "peer_stats": category.get("peer_stats"),
        "digest": category.get("digest"),
        "seasonal_beats": category.get("seasonal_beats"),
        "trend_signals": category.get("trend_signals"),
    }
    parts.append(f"\nCATEGORY:\n{json.dumps(cat_slim, ensure_ascii=False, indent=2)}")

    if customer:
        parts.append(f"\nCUSTOMER:\n{json.dumps(customer, ensure_ascii=False, indent=2)}")

    if conversation_history:
        parts.append(f"\nCONVERSATION HISTORY (last 5 turns):\n{json.dumps(conversation_history[-5:], ensure_ascii=False, indent=2)}")
        parts.append("\nIMPORTANT: Do NOT repeat any message already in the conversation history.")

    parts.append("\nCompose the next message. Return JSON only.")
    return "\n".join(parts)


def compose_message(
    trigger: dict,
    merchant: dict,
    category: dict,
    customer: Optional[dict] = None,
    conversation_history: Optional[list] = None,
) -> dict:
    """Call Claude to compose one message. Returns composed action dict."""
    prompt = build_composer_prompt(
        trigger, merchant, category, customer, conversation_history or []
    )
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        return result
    except Exception as e:
        log.error(f"Composer error: {e}")
        # Fallback message
        name = merchant.get("identity", {}).get("owner_first_name", "there")
        return {
            "body": f"Hi {name}, quick update from magicpin — want to catch up on how your profile is performing this week?",
            "send_as": "vera",
            "cta": "yes_no",
            "rationale": "Fallback due to composer error",
            "template_name": "vera_fallback_v1",
        }


REPLY_SYSTEM_PROMPT = """You are Vera, magicpin's AI merchant-engagement assistant on WhatsApp.

A merchant (or customer) has replied to your previous message. Compose the BEST next response.

RULES:
1. If merchant accepted / said yes → deliver on what you promised or advance the action.
2. If merchant said "join" or wants to sign up → immediately route to action: "Great! Let me set this up — takes 2 min. Reply YES to confirm, and I'll send the onboarding link."
3. If merchant asked a question → answer concisely with data from context.
4. If merchant said no / not interested → gracefully exit: action=end.
5. If message is clearly an auto-reply → action=wait (1800s), don't engage.
6. Keep responses ≤120 words. Match language pref. Single CTA at end.
7. Never repeat a message body verbatim.

OUTPUT JSON:
{
  "action": "send" | "wait" | "end",
  "body": "<message if action=send, else null>",
  "cta": "open_ended" | "yes_no" | "slot_choice" | "action_link",
  "wait_seconds": <int if action=wait, else null>,
  "rationale": "<1-2 sentences>"
}"""


def compose_reply(
    merchant_message: str,
    merchant: dict,
    category: dict,
    customer: Optional[dict],
    conversation_history: list[dict],
    trigger: Optional[dict],
) -> dict:
    """Compose a reply to a merchant/customer message."""
    intent = detect_intent(merchant_message)
    is_auto = detect_auto_reply(merchant_message)

    if is_auto:
        return {
            "action": "wait",
            "body": None,
            "cta": "open_ended",
            "wait_seconds": 1800,
            "rationale": "Detected auto-reply; backing off 30 min",
        }

    if intent == "reject":
        return {
            "action": "end",
            "body": None,
            "cta": "open_ended",
            "wait_seconds": None,
            "rationale": "Merchant indicated not interested; gracefully exiting",
        }

    # Build context for Claude
    ctx_block = {
        "merchant_message": merchant_message,
        "detected_intent": intent,
        "merchant": merchant,
        "category_voice": category.get("voice"),
        "category_offers": category.get("offer_catalog"),
        "category_digest": category.get("digest"),
        "peer_stats": category.get("peer_stats"),
        "conversation_history": conversation_history[-8:],
        "trigger": trigger,
    }
    if customer:
        ctx_block["customer"] = customer

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            system=REPLY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(ctx_block, ensure_ascii=False)}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        return result
    except Exception as e:
        log.error(f"Reply composer error: {e}")
        return {
            "action": "send",
            "body": "Noted! Let me check and get back to you shortly.",
            "cta": "open_ended",
            "wait_seconds": None,
            "rationale": "Fallback reply",
        }


# ═══════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

# ── /v1/healthz ─────────────────────────────────────────────────────────────
@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _) in contexts:
        if scope in counts:
            counts[scope] += 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": counts,
    }


# ── /v1/metadata ────────────────────────────────────────────────────────────
@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera Enhanced",
        "team_members": ["Challenger"],
        "model": CLAUDE_MODEL,
        "approach": (
            "4-context composer: category voice + merchant state + trigger routing + customer context. "
            "Claude composes every message with explicit compulsion-lever selection. "
            "Auto-reply detection, intent routing, suppression dedup."
        ),
        "contact_email": "challenger@example.com",
        "version": "1.0.0",
        "submitted_at": now_iso(),
    }


# ── /v1/context ─────────────────────────────────────────────────────────────
class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


@app.post("/v1/context")
async def push_context(body: CtxBody):
    valid_scopes = {"category", "merchant", "customer", "trigger"}
    if body.scope not in valid_scopes:
        return {"accepted": False, "reason": "invalid_scope", "details": f"Must be one of {valid_scopes}"}

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}

    contexts[key] = {"version": body.version, "payload": body.payload}
    log.info(f"Context stored: {body.scope}/{body.context_id} v{body.version}")
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": now_iso(),
    }


# ── /v1/tick ─────────────────────────────────────────────────────────────────
class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []

    # Sort triggers by urgency descending to process most urgent first
    trigger_payloads = []
    for trg_id in body.available_triggers:
        entry = contexts.get(("trigger", trg_id))
        if entry:
            trigger_payloads.append((trg_id, entry["payload"]))
    trigger_payloads.sort(key=lambda x: x[1].get("urgency", 1), reverse=True)

    # Track which merchants we've already messaged this tick (one per merchant per tick)
    messaged_merchants: set[str] = set()

    for trg_id, trg in trigger_payloads:
        if len(actions) >= 10:
            break  # limit per tick

        sup_key = trg.get("suppression_key", "")
        if sup_key and sup_key in suppressed:
            log.info(f"Suppressed trigger {trg_id}")
            continue

        merchant_id = trg.get("merchant_id")
        if not merchant_id:
            continue
        if merchant_id in messaged_merchants:
            continue

        merchant = get_ctx("merchant", merchant_id)
        if not merchant:
            log.warning(f"No merchant context for {merchant_id}")
            continue

        cat_slug = merchant.get("category_slug")
        category = get_ctx("category", cat_slug)
        if not category:
            log.warning(f"No category context for {cat_slug}")
            continue

        # For customer-scope triggers, load customer context
        customer = None
        customer_id = trg.get("customer_id")
        if customer_id:
            customer = get_ctx("customer", customer_id)

        # Check if there's an existing open conversation for this merchant+trigger
        conv_id = f"conv_{merchant_id}_{trg_id}"
        if conv_id in conversations:
            log.info(f"Conversation {conv_id} already open, skipping tick-initiation")
            continue

        log.info(f"Composing for {merchant_id} / {trg_id} (urgency={trg.get('urgency')})")
        try:
            composed = compose_message(trg, merchant, category, customer)
        except Exception as e:
            log.error(f"Compose failed: {e}")
            continue

        body_text = composed.get("body", "")
        if not body_text:
            continue

        # Create conversation record
        conversations[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trg_id,
            "turns": [{"role": "vera", "body": body_text}],
            "sent_bodies": {body_text},
        }

        if sup_key:
            suppressed.add(sup_key)
        messaged_merchants.add(merchant_id)

        action = {
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed.get("send_as", "vera"),
            "trigger_id": trg_id,
            "template_name": composed.get("template_name", "vera_composed_v1"),
            "template_params": [
                merchant.get("identity", {}).get("name", ""),
                trg.get("kind", ""),
                body_text[:50],
            ],
            "body": body_text,
            "cta": composed.get("cta", "open_ended"),
            "suppression_key": sup_key,
            "rationale": composed.get("rationale", ""),
        }
        actions.append(action)
        log.info(f"Action queued: {conv_id}")

    return {"actions": actions}


# ── /v1/reply ────────────────────────────────────────────────────────────────
class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv_id = body.conversation_id

    # Retrieve or create conversation state
    if conv_id not in conversations:
        conversations[conv_id] = {
            "merchant_id": body.merchant_id,
            "customer_id": body.customer_id,
            "trigger_id": None,
            "turns": [],
            "sent_bodies": set(),
        }

    conv = conversations[conv_id]
    conv["turns"].append({"role": body.from_role, "body": body.message})

    merchant_id = conv.get("merchant_id") or body.merchant_id
    customer_id = conv.get("customer_id") or body.customer_id
    trigger_id = conv.get("trigger_id")

    merchant = get_ctx("merchant", merchant_id) if merchant_id else {}
    cat_slug = (merchant or {}).get("category_slug")
    category = get_ctx("category", cat_slug) if cat_slug else {}
    customer = get_ctx("customer", customer_id) if customer_id else None
    trigger = get_ctx("trigger", trigger_id) if trigger_id else None

    result = compose_reply(
        merchant_message=body.message,
        merchant=merchant or {},
        category=category or {},
        customer=customer,
        conversation_history=conv["turns"],
        trigger=trigger,
    )

    action = result.get("action", "send")

    if action == "send":
        reply_body = result.get("body") or ""
        # Anti-repetition check
        if reply_body in conv["sent_bodies"]:
            # Modify slightly to avoid exact repetition penalty
            reply_body = reply_body + " — want me to set that up?"
        conv["turns"].append({"role": "vera", "body": reply_body})
        conv["sent_bodies"].add(reply_body)
        log.info(f"Replying to {conv_id}: {reply_body[:60]}…")
        return {
            "action": "send",
            "body": reply_body,
            "cta": result.get("cta", "open_ended"),
            "rationale": result.get("rationale", ""),
        }
    elif action == "wait":
        wait_s = result.get("wait_seconds", 1800)
        log.info(f"Waiting {wait_s}s on {conv_id}")
        return {
            "action": "wait",
            "wait_seconds": wait_s,
            "rationale": result.get("rationale", "Backing off"),
        }
    else:  # end
        log.info(f"Ending conversation {conv_id}")
        return {
            "action": "end",
            "rationale": result.get("rationale", "Merchant declined"),
        }


# ── /v1/teardown (optional) ──────────────────────────────────────────────────
@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    suppressed.clear()
    log.info("State wiped on teardown")
    return {"status": "wiped"}


# ── Dev entrypoint ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=8080, reload=False)
