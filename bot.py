#!/usr/bin/env python3
"""
Vera Bot — magicpin AI Challenge Submission
Gemini-powered merchant engagement assistant.

Run: uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import os
import time
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from google import genai
from google.genai import types
from fastapi import FastAPI
from pydantic import BaseModel

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vera_bot")

# Gemini config
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable is not set!")

def call_gemini(system_prompt: str, user_prompt: str, max_tokens: int = 512) -> str:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=f"{system_prompt}\n\n{user_prompt}",
        config=types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=0.7,
        ),
    )
    return response.text.strip()

START_TIME = time.time()
contexts: dict[tuple[str, str], dict] = {}
conversations: dict[str, dict] = {}
suppressed: set[str] = set()

app = FastAPI(title="Vera Bot (Gemini)", version="1.0.0")

def get_ctx(scope: str, context_id: str):
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def detect_auto_reply(message: str) -> bool:
    patterns = ["thank you for contacting","thanks for contacting","we have received your message",
                "we will get back to you","our team will respond","this is an automated","auto reply",
                "आपका संदेश प्राप्त हुआ","हम जल्द ही"]
    return any(p in message.lower().strip() for p in patterns)

def detect_intent(message: str) -> str:
    low = message.lower().strip()
    for p in ["join","judrna","jodna","sign up","subscribe","enroll","register"]:
        if p in low: return "join_intent"
    for p in ["yes","haan","ok","okay","go ahead","send it","send me","sure","bilkul","zaroor","theek","done","let's do","lets do","go"]:
        if low.startswith(p) or f" {p}" in low or low == p: return "accept"
    for p in ["no","nahi","nope","not interested","stop","don't","later","baad mein"]:
        if p in low: return "reject"
    return "question_or_other"

def clean_json(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"): raw = raw[4:]
    return raw.strip()

COMPOSE_SYSTEM = """You are Vera, magicpin's AI merchant-engagement assistant. You talk to merchants over WhatsApp.

MISSION: Compose ONE proactive message based on the 4-context framework provided.

GOLDEN RULES:
1. Max 160 words. Ideal 80-120. WhatsApp style, not email.
2. Single clear CTA in the LAST sentence only.
3. No preamble. Get straight to the point.
4. Match voice: dentists->peer/clinical, restaurants->warm/operator, gyms->energetic/coach, pharmacies->precise/trustworthy, salons->warm/practical.
5. Use service+price offers ("Haircut @ Rs 99") NOT generic discounts ("flat 30% off").
6. Dentists/pharmacies: NEVER use "cure", "guaranteed", "best in city".
7. Honor language preference: hi-en mix->mix Hindi+English. hi->Hindi. english->English only.
8. Use exactly ONE compulsion lever: specificity/data OR loss-aversion OR social-proof OR effort-externalization OR curiosity OR reciprocity OR asking-merchant OR single-binary-CTA.
9. customer-scope trigger -> send_as = "merchant_on_behalf"
10. merchant-scope trigger -> send_as = "vera"
11. NEVER invent data. Only cite sources in the digest.
12. urgency 4-5 -> convey urgency without being alarmist.

OUTPUT: Valid JSON only. No markdown.
{"body":"<message>","send_as":"vera","cta":"open_ended","rationale":"<1-2 sentences>","template_name":"<slug_v1>"}"""

REPLY_SYSTEM = """You are Vera, magicpin's AI merchant-engagement assistant on WhatsApp.

A merchant or customer just replied. Decide the best next move.

RULES:
1. yes/accept -> deliver what was promised, advance the action.
2. "join"/sign up -> action=send: "Great! Takes 2 min — reply YES to confirm and I'll send the onboarding link."
3. question -> answer concisely using context data only.
4. no/not interested -> action=end.
5. auto-reply -> action=wait 1800 seconds.
6. Under 120 words. Match language. Single CTA at end.
7. Never repeat previous message verbatim.

OUTPUT: Valid JSON only. No markdown.
{"action":"send","body":"<message or null>","cta":"open_ended","wait_seconds":null,"rationale":"<1-2 sentences>"}"""

def compose_message(trigger, merchant, category, customer=None, conversation_history=None):
    cat_slim = {k: category.get(k) for k in ["slug","voice","offer_catalog","peer_stats","digest","seasonal_beats","trend_signals"]}
    parts = [
        f"TRIGGER:\n{json.dumps(trigger, ensure_ascii=False, indent=2)}",
        f"\nMERCHANT:\n{json.dumps(merchant, ensure_ascii=False, indent=2)}",
        f"\nCATEGORY:\n{json.dumps(cat_slim, ensure_ascii=False, indent=2)}",
    ]
    if customer:
        parts.append(f"\nCUSTOMER:\n{json.dumps(customer, ensure_ascii=False, indent=2)}")
    if conversation_history:
        parts.append(f"\nCONVERSATION HISTORY:\n{json.dumps((conversation_history or [])[-5:], ensure_ascii=False, indent=2)}")
        parts.append("\nDo NOT repeat any message already in the history.")
    parts.append("\nCompose the next message. Return JSON only.")
    try:
        raw = call_gemini(COMPOSE_SYSTEM, "\n".join(parts), max_tokens=512)
        return json.loads(clean_json(raw))
    except Exception as e:
        log.error(f"Compose error: {e}")
        name = merchant.get("identity", {}).get("owner_first_name", "there")
        return {"body": f"Hi {name}, quick update from magicpin — want to catch up on how your profile is performing this week?",
                "send_as": "vera", "cta": "yes_no", "rationale": "Fallback due to composer error", "template_name": "vera_fallback_v1"}

def compose_reply(merchant_message, merchant, category, customer, conversation_history, trigger):
    intent = detect_intent(merchant_message)
    if detect_auto_reply(merchant_message):
        return {"action":"wait","body":None,"cta":"open_ended","wait_seconds":1800,"rationale":"Auto-reply detected"}
    if intent == "reject":
        return {"action":"end","body":None,"cta":"open_ended","wait_seconds":None,"rationale":"Merchant not interested"}
    ctx_block = {"merchant_message": merchant_message, "detected_intent": intent,
                 "merchant_name": merchant.get("identity",{}).get("name"),
                 "language_pref": merchant.get("identity",{}).get("languages",["en"]),
                 "category_voice": category.get("voice"), "category_offers": category.get("offer_catalog"),
                 "category_digest": category.get("digest"), "peer_stats": category.get("peer_stats"),
                 "conversation_history": conversation_history[-8:], "trigger": trigger}
    if customer: ctx_block["customer"] = customer
    try:
        raw = call_gemini(REPLY_SYSTEM, json.dumps(ctx_block, ensure_ascii=False), max_tokens=400)
        return json.loads(clean_json(raw))
    except Exception as e:
        log.error(f"Reply compose error: {e}")
        return {"action":"send","body":"Got it! Let me check and get back to you shortly.","cta":"open_ended","wait_seconds":None,"rationale":"Fallback reply"}

@app.get("/v1/healthz")
async def healthz():
    counts = {"category":0,"merchant":0,"customer":0,"trigger":0}
    for (scope,_) in contexts:
        if scope in counts: counts[scope] += 1
    return {"status":"ok","uptime_seconds":int(time.time()-START_TIME),"contexts_loaded":counts}

@app.get("/v1/metadata")
async def metadata():
    return {"team_name":"Vera Enhanced","team_members":["Challenger"],"model":GEMINI_MODEL,
            "approach":"4-context composer with Gemini 1.5 Flash. Auto-reply detection, intent routing, suppression dedup.",
            "contact_email":"challenger@example.com","version":"1.0.0","submitted_at":now_iso()}

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str

@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in {"category","merchant","customer","trigger"}:
        return {"accepted":False,"reason":"invalid_scope"}
    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted":False,"reason":"stale_version","current_version":cur["version"]}
    contexts[key] = {"version":body.version,"payload":body.payload}
    log.info(f"Stored: {body.scope}/{body.context_id} v{body.version}")
    return {"accepted":True,"ack_id":f"ack_{body.context_id}_v{body.version}","stored_at":now_iso()}

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []

@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    trigger_payloads = []
    for trg_id in body.available_triggers:
        entry = contexts.get(("trigger", trg_id))
        if entry: trigger_payloads.append((trg_id, entry["payload"]))
    trigger_payloads.sort(key=lambda x: x[1].get("urgency",1), reverse=True)
    messaged_merchants: set[str] = set()
    for trg_id, trg in trigger_payloads:
        if len(actions) >= 10: break
        sup_key = trg.get("suppression_key","")
        if sup_key and sup_key in suppressed: continue
        merchant_id = trg.get("merchant_id")
        if not merchant_id or merchant_id in messaged_merchants: continue
        merchant = get_ctx("merchant", merchant_id)
        if not merchant: continue
        category = get_ctx("category", merchant.get("category_slug"))
        if not category: continue
        customer = get_ctx("customer", trg.get("customer_id")) if trg.get("customer_id") else None
        conv_id = f"conv_{merchant_id}_{trg_id}"
        if conv_id in conversations: continue
        log.info(f"Composing for {merchant_id} / {trg_id} urgency={trg.get('urgency')}")
        try:
            composed = compose_message(trg, merchant, category, customer)
        except Exception as e:
            log.error(f"Compose failed: {e}"); continue
        body_text = composed.get("body","")
        if not body_text: continue
        conversations[conv_id] = {"merchant_id":merchant_id,"customer_id":trg.get("customer_id"),
                                   "trigger_id":trg_id,"turns":[{"role":"vera","body":body_text}],"sent_bodies":{body_text}}
        if sup_key: suppressed.add(sup_key)
        messaged_merchants.add(merchant_id)
        actions.append({"conversation_id":conv_id,"merchant_id":merchant_id,"customer_id":trg.get("customer_id"),
                        "send_as":composed.get("send_as","vera"),"trigger_id":trg_id,
                        "template_name":composed.get("template_name","vera_gemini_v1"),
                        "template_params":[merchant.get("identity",{}).get("name",""),trg.get("kind",""),body_text[:50]],
                        "body":body_text,"cta":composed.get("cta","open_ended"),
                        "suppression_key":sup_key,"rationale":composed.get("rationale","")})
        log.info(f"Action queued: {conv_id}")
    return {"actions":actions}

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
    if conv_id not in conversations:
        conversations[conv_id] = {"merchant_id":body.merchant_id,"customer_id":body.customer_id,
                                   "trigger_id":None,"turns":[],"sent_bodies":set()}
    conv = conversations[conv_id]
    conv["turns"].append({"role":body.from_role,"body":body.message})
    merchant_id = conv.get("merchant_id") or body.merchant_id
    customer_id = conv.get("customer_id") or body.customer_id
    merchant = get_ctx("merchant", merchant_id) if merchant_id else {}
    category = get_ctx("category", (merchant or {}).get("category_slug")) if merchant else {}
    customer = get_ctx("customer", customer_id) if customer_id else None
    trigger = get_ctx("trigger", conv.get("trigger_id")) if conv.get("trigger_id") else None
    result = compose_reply(body.message, merchant or {}, category or {}, customer, conv["turns"], trigger)
    action = result.get("action","send")
    if action == "send":
        reply_body = result.get("body") or ""
        if reply_body in conv["sent_bodies"]: reply_body += " — shall I set that up now?"
        conv["turns"].append({"role":"vera","body":reply_body})
        conv["sent_bodies"].add(reply_body)
        return {"action":"send","body":reply_body,"cta":result.get("cta","open_ended"),"rationale":result.get("rationale","")}
    elif action == "wait":
        return {"action":"wait","wait_seconds":result.get("wait_seconds",1800),"rationale":result.get("rationale","Backing off")}
    else:
        return {"action":"end","rationale":result.get("rationale","Merchant declined")}

@app.post("/v1/teardown")
async def teardown():
    contexts.clear(); conversations.clear(); suppressed.clear()
    return {"status":"wiped"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=8080, reload=False)
