"""
Vera Bot - Gemini-Powered Message Composer
============================================
This module takes the 4 context layers (category, merchant, trigger, customer)
and uses Google Gemini REST API to compose a WhatsApp message.

The prompt is engineered to score high on the 5 judge dimensions:
  1. Specificity     - concrete numbers, dates, citations
  2. Category fit    - voice, vocabulary, tone matching
  3. Merchant fit    - personalized to this merchant's data
  4. Trigger relevance - clearly explains "why now"
  5. Engagement compulsion - curiosity, social proof, loss aversion, etc.
"""

import json
import time
import logging
import re
import urllib.request
import urllib.error

logger = logging.getLogger("vera-bot")

# ============================================================================
# GEMINI CLIENT SETUP
# ============================================================================

_api_key = None
FALLBACK_MODELS = [
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
]


def init_gemini(api_key: str):
    """Initialize the Gemini API key."""
    global _api_key
    _api_key = api_key
    logger.info("Gemini REST initialized with active API key")


# ============================================================================
# SYSTEM PROMPT (constant - defines Vera's persona and rules)
# ============================================================================

SYSTEM_PROMPT = """You are Vera, magicpin's AI merchant assistant that talks to merchants over WhatsApp.

## YOUR CORE RULES (never violate these):
1. NEVER fabricate data. Only use facts from the provided contexts.
2. NEVER use taboo vocabulary listed in the category voice profile.
3. NEVER use generic offers like "Flat 30% off". Always use service+price format like "Haircut @ Rs.99".
4. NEVER include multiple CTAs. One clear call-to-action only, at the END.
5. NEVER start with long preambles like "I hope you're doing well".
6. NEVER use ALL CAPS or excessive exclamation marks for promotional tone.
7. Keep messages concise. WhatsApp is a chat, not an email.
8. Match the merchant's language preference (Hindi-English code-mix when indicated).

## ENGAGEMENT LEVERS (use 1-2 per message):
- Specificity: Anchor on a verifiable number, date, or citation.
- Loss aversion: "you're missing X" / "before this window closes"
- Social proof: "3 dentists in your locality did Y this month"
- Effort externalization: "I've drafted X - just say GO"
- Curiosity: "want to see who?" / "want the full list?"
- Reciprocity: "I noticed Y about your account, thought you'd want to know"
- Asking the merchant: "what's your most-asked service this week?"

## CTA TYPES:
- "binary_yes_no": For action triggers. End with "Reply YES to go / STOP to skip"
- "open_ended": For information/curiosity triggers. End with an open question.
- "multi_choice_slot": For booking flows. Offer numbered slot options.
- "none": For pure information where no response needed.

## SEND_AS RULES:
- "vera": When messaging the merchant directly (scope=merchant)
- "merchant_on_behalf": When messaging a customer on behalf of the merchant (scope=customer)
"""


# ============================================================================
# TRIGGER-SPECIFIC STRATEGIES
# ============================================================================

TRIGGER_STRATEGIES = {
    "research_digest": {
        "approach": "Lead with the research finding. Cite source, trial size, key stat. Offer to pull the abstract or draft patient-facing content.",
        "levers": ["specificity", "curiosity", "reciprocity"],
        "cta_type": "open_ended",
    },
    "regulation_change": {
        "approach": "Lead with the deadline and what changed. Be factual, cite the authority. Offer to help audit compliance.",
        "levers": ["specificity", "loss_aversion"],
        "cta_type": "binary_yes_no",
    },
    "recall_due": {
        "approach": "Customer-facing recall reminder. Name the patient, mention time since last visit, offer specific slots matching their preference. Include the service price.",
        "levers": ["specificity", "effort_externalization"],
        "cta_type": "multi_choice_slot",
    },
    "perf_dip": {
        "approach": "Flag the drop with exact numbers (metric, percentage, timeframe). Diagnose a possible cause. Offer a concrete fix.",
        "levers": ["specificity", "loss_aversion", "effort_externalization"],
        "cta_type": "binary_yes_no",
    },
    "perf_spike": {
        "approach": "Celebrate the improvement with exact numbers. Attribute the likely cause. Suggest how to sustain/amplify it.",
        "levers": ["specificity", "reciprocity", "curiosity"],
        "cta_type": "open_ended",
    },
    "milestone_reached": {
        "approach": "Congratulate with the exact milestone number. Suggest a next milestone or how to celebrate/leverage it.",
        "levers": ["social_proof", "curiosity"],
        "cta_type": "open_ended",
    },
    "festival_upcoming": {
        "approach": "Connect the festival to the merchant's category. Suggest a timely campaign or offer. Be specific about dates.",
        "levers": ["specificity", "effort_externalization"],
        "cta_type": "binary_yes_no",
    },
    "ipl_match_today": {
        "approach": "Connect today's match to footfall opportunity. Be specific about match details, timing, and suggested action.",
        "levers": ["specificity", "loss_aversion", "effort_externalization"],
        "cta_type": "binary_yes_no",
    },
    "review_theme_emerged": {
        "approach": "Flag the review theme with exact count and a real customer quote. Suggest a fix without being preachy.",
        "levers": ["specificity", "social_proof", "reciprocity"],
        "cta_type": "open_ended",
    },
    "renewal_due": {
        "approach": "State days remaining and what will pause when it expires. Show what the merchant has built (stats). Make renewal easy.",
        "levers": ["loss_aversion", "specificity", "effort_externalization"],
        "cta_type": "binary_yes_no",
    },
    "winback_eligible": {
        "approach": "Acknowledge the gap. Show what changed since they left (missed views, competitors). Low-pressure re-engagement.",
        "levers": ["loss_aversion", "specificity", "curiosity"],
        "cta_type": "open_ended",
    },
    "dormant_with_vera": {
        "approach": "Light, non-pushy re-engagement. Ask a genuine question about their business. Don't lecture.",
        "levers": ["curiosity", "asking_the_merchant"],
        "cta_type": "open_ended",
    },
    "competitor_opened": {
        "approach": "Factually note the new competitor (name, distance, their offer). Position the merchant's strengths. Suggest a response.",
        "levers": ["loss_aversion", "specificity", "effort_externalization"],
        "cta_type": "binary_yes_no",
    },
    "supply_alert": {
        "approach": "Urgent safety/compliance alert. Be factual, list affected items, offer to help filter impacted customers.",
        "levers": ["specificity", "effort_externalization"],
        "cta_type": "binary_yes_no",
    },
    "customer_lapsed_hard": {
        "approach": "Customer-facing winback. Mention what they used to enjoy, offer an incentive to return. Keep it warm, not desperate.",
        "levers": ["specificity", "effort_externalization"],
        "cta_type": "binary_yes_no",
    },
    "active_planning_intent": {
        "approach": "The merchant asked for help planning something. Respond with a concrete draft/proposal. Be actionable immediately.",
        "levers": ["effort_externalization", "specificity"],
        "cta_type": "binary_yes_no",
    },
    "curious_ask_due": {
        "approach": "Ask the merchant a genuine, business-relevant question. No selling, just curiosity and relationship building.",
        "levers": ["asking_the_merchant", "curiosity"],
        "cta_type": "open_ended",
    },
    "seasonal_perf_dip": {
        "approach": "Acknowledge the seasonal dip as normal. Suggest counter-seasonal strategies to maintain engagement.",
        "levers": ["specificity", "social_proof", "effort_externalization"],
        "cta_type": "open_ended",
    },
    "category_seasonal": {
        "approach": "Flag seasonal demand shifts with specific trend data. Suggest inventory/promotion adjustments.",
        "levers": ["specificity", "loss_aversion"],
        "cta_type": "binary_yes_no",
    },
    "gbp_unverified": {
        "approach": "Explain verification simply. Quantify the uplift they'd get. Offer to walk them through it.",
        "levers": ["specificity", "loss_aversion", "effort_externalization"],
        "cta_type": "binary_yes_no",
    },
    "cde_opportunity": {
        "approach": "Share the CDE/webinar opportunity with date, speaker, credits. Keep it peer-level, not salesy.",
        "levers": ["specificity", "reciprocity"],
        "cta_type": "open_ended",
    },
    "chronic_refill_due": {
        "approach": "Customer-facing refill reminder. List medicines, stock-out date, offer delivery. Be helpful, not alarming.",
        "levers": ["specificity", "effort_externalization"],
        "cta_type": "binary_yes_no",
    },
    "trial_followup": {
        "approach": "Customer-facing trial followup. Reference the trial they attended, offer next session with specific time.",
        "levers": ["specificity", "effort_externalization"],
        "cta_type": "binary_yes_no",
    },
    "wedding_package_followup": {
        "approach": "Customer-facing bridal followup. Reference wedding date, suggest next step in the prep timeline.",
        "levers": ["specificity", "effort_externalization"],
        "cta_type": "open_ended",
    },
}

DEFAULT_STRATEGY = {
    "approach": "Analyze the trigger and compose a relevant, specific message using the merchant's data.",
    "levers": ["specificity", "curiosity"],
    "cta_type": "open_ended",
}


# ============================================================================
# PROMPT BUILDER
# ============================================================================

def build_compose_prompt(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: dict | None = None,
) -> str:
    """Build user prompt for Gemini by injecting all 4 context layers."""
    trigger_kind = trigger.get("kind", "unknown")
    strategy = TRIGGER_STRATEGIES.get(trigger_kind, DEFAULT_STRATEGY)

    is_customer_facing = trigger.get("scope") == "customer" and customer is not None
    send_as = "merchant_on_behalf" if is_customer_facing else "vera"

    sections = []

    sections.append(f"""## CATEGORY CONTEXT
Category: {category.get('slug', 'unknown')} ({category.get('display_name', '')})
Voice tone: {json.dumps(category.get('voice', {}), indent=2)}
Offer catalog: {json.dumps(category.get('offer_catalog', []), indent=2)}
Peer stats: {json.dumps(category.get('peer_stats', {}), indent=2)}
Digest (latest research/news): {json.dumps(category.get('digest', []), indent=2)}
Seasonal beats: {json.dumps(category.get('seasonal_beats', []), indent=2)}
Trend signals: {json.dumps(category.get('trend_signals', []), indent=2)}
Patient content library: {json.dumps(category.get('patient_content_library', []), indent=2)}""")

    sections.append(f"""## MERCHANT CONTEXT
Merchant ID: {merchant.get('merchant_id', 'unknown')}
{json.dumps(merchant, indent=2)}""")

    sections.append(f"""## TRIGGER CONTEXT (reason for messaging RIGHT NOW)
Trigger ID: {trigger.get('id', 'unknown')}
Kind: {trigger_kind}
Scope: {trigger.get('scope', 'merchant')}
Source: {trigger.get('source', 'unknown')}
Urgency: {trigger.get('urgency', 1)}/5
Payload: {json.dumps(trigger.get('payload', {}), indent=2)}
Suppression key: {trigger.get('suppression_key', '')}""")

    if is_customer_facing and customer:
        sections.append(f"""## CUSTOMER CONTEXT (messaging customer ON BEHALF of merchant)
{json.dumps(customer, indent=2)}""")

    sections.append(f"""## YOUR STRATEGY FOR THIS TRIGGER
Trigger kind: {trigger_kind}
Approach: {strategy['approach']}
Engagement levers to use: {', '.join(strategy['levers'])}
Recommended CTA type: {strategy['cta_type']}
Send as: {send_as}""")

    sections.append("""## REQUIRED OUTPUT FORMAT
Respond ONLY with a valid JSON object with these exact keys:
{
  "body": "The WhatsApp message body text. Concise, specific, voice-matched.",
  "cta": "One of: binary_yes_no, open_ended, multi_choice_slot, binary_confirm_cancel, none",
  "send_as": "vera or merchant_on_behalf",
  "suppression_key": "Copy from the trigger suppression_key",
  "rationale": "2-3 sentences: why this message, which engagement levers used, what you expect to achieve"
}

IMPORTANT:
- Use REAL numbers from merchant data (views, calls, CTR, days, etc.)
- Cite REAL sources from digest (journal name, page number)
- Match merchant language preference from identity.languages (e.g. natural Hindi-English code-mixing)
- Put CTA at the END of the message
- Do NOT use generic marketing phrases or URLs
- Output JSON ONLY""")

    return "\n\n".join(sections)


# ============================================================================
# REST CALL TO GEMINI
# ============================================================================

def call_gemini_rest(full_prompt: str) -> str | None:
    """Call Gemini generateContent via REST API with fallback models."""
    if not _api_key:
        logger.error("Gemini API key is not configured.")
        return None

    request_payload = {
        "contents": [
            {
                "parts": [
                    {"text": full_prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json"
        }
    }
    body_bytes = json.dumps(request_payload).encode("utf-8")

    for model_name in FALLBACK_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={_api_key}"
        
        for attempt in range(2):
            req = urllib.request.Request(
                url,
                data=body_bytes,
                headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    res_data = json.loads(resp.read().decode("utf-8"))
                    candidates = res_data.get("candidates", [])
                    if candidates:
                        text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        if text:
                            return text
            except urllib.error.HTTPError as e:
                err_msg = e.read().decode("utf-8", errors="ignore")
                if e.code == 429 and attempt == 0:
                    logger.info(f"Model {model_name} rate limited (429). Quick backoff 2s...")
                    time.sleep(2)
                    continue
                else:
                    logger.warning(f"Model {model_name} HTTP {e.code}: {err_msg[:150]}")
                    break
            except Exception as e:
                logger.warning(f"Model {model_name} failed: {type(e).__name__}: {e}")
                break

    logger.error("Gemini call timed out or rate limited.")
    return None


# ============================================================================
# COMPOSE MESSAGE (main entry point)
# ============================================================================

def compose_message(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: dict | None = None,
) -> dict | None:
    """Compose a WhatsApp message using Gemini REST."""
    user_prompt = build_compose_prompt(category, merchant, trigger, customer)
    full_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"

    raw_text = call_gemini_rest(full_prompt)
    if not raw_text:
        return None

    cleaned_text = raw_text.strip()
    if cleaned_text.startswith("```"):
        cleaned_text = re.sub(r"^```(?:json)?\s*", "", cleaned_text)
        cleaned_text = re.sub(r"\s*```$", "", cleaned_text)

    result = {}
    try:
        result = json.loads(cleaned_text)
    except Exception:
        # Robust regex fallback extraction
        body_match = re.search(r'"body":\s*"([^"]+)', cleaned_text)
        cta_match = re.search(r'"cta":\s*"([^"]+)', cleaned_text)
        send_as_match = re.search(r'"send_as":\s*"([^"]+)', cleaned_text)
        rationale_match = re.search(r'"rationale":\s*"([^"]+)', cleaned_text)

        if body_match:
            result["body"] = body_match.group(1).replace("\\n", "\n").replace('\\"', '"')
        if cta_match:
            result["cta"] = cta_match.group(1)
        if send_as_match:
            result["send_as"] = send_as_match.group(1)
        if rationale_match:
            result["rationale"] = rationale_match.group(1)

    if not result.get("body"):
        return None

    # Ensure required keys
    result.setdefault("cta", "open_ended")
    result.setdefault("send_as", "vera")
    result["suppression_key"] = trigger.get("suppression_key", result.get("suppression_key", ""))
    result.setdefault("rationale", "Auto-composed message")

    valid_ctas = {"binary_yes_no", "open_ended", "multi_choice_slot", "binary_confirm_cancel", "none"}
    if result.get("cta") not in valid_ctas:
        result["cta"] = "open_ended"

    if result.get("send_as") not in {"vera", "merchant_on_behalf"}:
        result["send_as"] = "vera"

    logger.info(f"Message composed for trigger={trigger.get('id')} | cta={result['cta']}")
    return result
