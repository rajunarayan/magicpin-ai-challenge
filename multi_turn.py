"""
Vera Bot - Multi-Turn Conversation Handler
===========================================
Handles incoming replies from merchants and customers on /v1/reply.

Capabilities:
  1. Auto-reply detection (canned WhatsApp Business auto-replies)
  2. Explicit opt-out / "stop" detection (graceful exit)
  3. Intent transition handling (pitch mode -> action mode immediately)
  4. Out-of-scope redirection (e.g. GST, legal, tax)
  5. Multi-turn AI response generation via Gemini
"""

import re
import json
import logging
from composer import call_gemini_rest, SYSTEM_PROMPT

logger = logging.getLogger("vera-bot")

# ============================================================================
# PATTERNS FOR FAST CLASSIFICATION
# ============================================================================

AUTO_REPLY_PATTERNS = [
    r"thank\s+you\s+for\s+contacting",
    r"our\s+team\s+will\s+respond\s+shortly",
    r"we\s+are\s+currently\s+(away|unavailable|closed)",
    r"this\s+is\s+an\s+automated\s+(response|message|reply)",
    r"automated\s+assistant",
    r"hamari\s+team\s+tak\s+pahuncha",
    r"aapki\s+jaankari\s+ke\s+liye\s+bahut\s+shukriya",
    r"auto-reply",
]

COMPILED_AUTO_REPLY = [re.compile(p, re.IGNORECASE) for p in AUTO_REPLY_PATTERNS]

OPT_OUT_PATTERNS = [
    r"^stop\b",
    r"^unsubscribe\b",
    r"not\s+interested",
    r"stop\s+messaging",
    r"don('?t|t)\s+message",
    r"bothering\s+me",
    r"mat\s+bhejo",
    r"leave\s+me\s+alone",
    r"block",
]

COMPILED_OPT_OUT = [re.compile(p, re.IGNORECASE) for p in OPT_OUT_PATTERNS]


def is_auto_reply(text: str) -> bool:
    """Check if the message matches known canned auto-reply phrasing."""
    if not isinstance(text, str):
        return False
    return any(p.search(text) for p in COMPILED_AUTO_REPLY)


def is_opt_out(text: str) -> bool:
    """Check if merchant is explicitly asking to stop."""
    if not isinstance(text, str):
        return False
    return any(p.search(text) for p in COMPILED_OPT_OUT)


# ============================================================================
# MULTI-TURN REPLY HANDLER
# ============================================================================

def handle_reply_message(
    conversation: dict,
    merchant: dict,
    category: dict,
    merchant_message: str,
    turn_number: int = 2,
) -> dict:
    """
    Decide action and generate response for an incoming message.

    Returns dict with keys:
      - action: "send" | "wait" | "end"
      - body (if action == "send")
      - cta (if action == "send")
      - wait_seconds (if action == "wait")
      - rationale
    """
    history = conversation.get("history", [])

    # Check repetition of incoming message across turns (characteristic of bot loops)
    incoming_history = [h.get("body", "") for h in history if h.get("from") != "vera"]
    is_repeated_auto_reply = incoming_history.count(merchant_message) >= 1

    # ------------------------------------------------------------------------
    # 1. OPT-OUT / HOSTILE SIGNAL -> Immediate Graceful Exit
    # ------------------------------------------------------------------------
    if is_opt_out(merchant_message):
        logger.info(f"Opt-out detected: '{merchant_message[:40]}'")
        return {
            "action": "end",
            "rationale": "Merchant explicitly requested to stop / expressed disinterest. Closed conversation gracefully."
        }

    # ------------------------------------------------------------------------
    # 2. AUTO-REPLY DETECTION
    # ------------------------------------------------------------------------
    if is_auto_reply(merchant_message) or is_repeated_auto_reply:
        logger.info(f"Auto-reply detected on turn {turn_number}: '{merchant_message[:40]}'")
        if turn_number <= 1 and not is_repeated_auto_reply:
            # Turn 1 auto-reply: Send a gentle flag for the owner
            owner_name = merchant.get("identity", {}).get("owner_first_name", "")
            salutation = f"Dr. {owner_name}" if category.get("slug") == "dentists" else (owner_name or "there")
            return {
                "action": "send",
                "body": f"Looks like an auto-reply :) Whenever {salutation} sees this, just reply 'Yes' or tell me what works best for you.",
                "cta": "binary_yes_no",
                "rationale": "Auto-reply detected. Providing low-friction re-entry prompt for the business owner."
            }
        elif turn_number <= 3 or is_repeated_auto_reply:
            # Turn 2-3 auto-reply: Back off 4 hours
            return {
                "action": "wait",
                "wait_seconds": 14400,
                "rationale": "Auto-reply detected again. Backing off 4 hours to wait for the actual business owner."
            }
        else:
            # Turn 4+ auto-reply: Close conversation
            return {
                "action": "end",
                "rationale": "Repeated auto-reply with no human interaction. Gracefully ending conversation."
            }

    # ------------------------------------------------------------------------
    # 3. CONVERSATIONAL REASONING WITH GEMINI
    # ------------------------------------------------------------------------
    # Build context for multi-turn continuation
    convo_transcript = "\n".join(
        [f"- [{h.get('from', 'user').upper()}]: {h.get('body', '')}" for h in history]
    )
    convo_transcript += f"\n- [MERCHANT]: {merchant_message}"

    prompt = f"""## MULTI-TURN CONVERSATION
Merchant: {merchant.get('identity', {}).get('name', '')} ({merchant.get('category_slug', '')})
Languages: {merchant.get('identity', {}).get('languages', ['en'])}
Category voice rules: {json.dumps(category.get('voice', {}), indent=2)}

## CONVERSATION HISTORY SO FAR:
{convo_transcript}

## MERCHANT'S LATEST MESSAGE (Turn {turn_number}):
"{merchant_message}"

## INSTRUCTIONS:
1. If the merchant explicitly committed / agreed ("yes", "send it", "draft it", "let's do it"), IMMEDIATELY switch to action execution mode. Do not ask more qualification questions. Provide the deliverable/draft directly and ask for binary confirmation.
2. If the merchant asked an out-of-scope question (e.g. GST filing, legal advice, taxes), politely clarify that is outside what Vera handles, and smoothly redirect back to the current proposal.
3. If the merchant asked a relevant question, answer it directly with facts from the category/merchant context.
4. Keep the message concise and natural for WhatsApp.
5. Respect language preferences (Hindi-English mix if used by merchant).

## OUTPUT FORMAT:
Respond ONLY with a JSON object:
{{
  "action": "send",
  "body": "Your WhatsApp reply text",
  "cta": "binary_yes_no OR open_ended OR binary_confirm_cancel OR none",
  "rationale": "2 sentences explaining the tactical approach taken in this turn"
}}
"""

    raw_response = call_gemini_rest(f"{SYSTEM_PROMPT}\n\n{prompt}")
    if raw_response:
        try:
            cleaned = raw_response.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
                cleaned = re.sub(r"\s*```$", "", cleaned)
            parsed = json.loads(cleaned)
            parsed.setdefault("action", "send")
            parsed.setdefault("cta", "binary_yes_no")
            parsed.setdefault("rationale", "Addressed merchant inquiry and advanced conversation.")
            return parsed
        except Exception as e:
            logger.error(f"Error parsing multi-turn reply JSON: {e}")

    # Safe fallback if LLM call is interrupted
    return {
        "action": "send",
        "body": "Got it! I am putting this together for you right away. Should I send you the draft to review first?",
        "cta": "binary_yes_no",
        "rationale": "Fallbackcknowledged merchant request with clear action confirmation."
    }
