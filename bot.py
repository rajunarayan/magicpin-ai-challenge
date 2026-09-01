"""
magicpin AI Challenge - Vera Bot Server
========================================
A secure Flask server with hardened endpoints for the AI judge.

Security measures:
  1. API token authentication (only judge can call endpoints)
  2. Input validation & sanitization on all POST endpoints
  3. Request size limits (reject oversized payloads)
  4. Rate limiting per IP
  5. Prompt injection protection (sanitize data before sending to Gemini)
  6. Safe error handling (no stack traces leaked to clients)
  7. Store size caps (prevent memory exhaustion)
  8. Strict CORS (no cross-origin access)
  9. Only allowed HTTP methods per route
  10. Security headers on all responses
"""

import os
import re
import time
import json
import logging
import functools
from datetime import datetime, timezone
from collections import defaultdict
from flask import Flask, request, jsonify, g
from dotenv import load_dotenv
from composer import init_gemini, compose_message
from multi_turn import handle_reply_message

# Load API key from .env file
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BOT_API_TOKEN = os.getenv("BOT_API_TOKEN", "")  # Optional: judge uses this to authenticate

if GEMINI_API_KEY:
    init_gemini(GEMINI_API_KEY)


def compose(category: dict, merchant: dict, trigger: dict, customer: dict = None) -> dict:
    """
    Standalone composition entry point conforming to Challenge Brief §7.1.
    """
    return compose_message(category, merchant, trigger, customer)

# ============================================================================
# SECURITY CONFIGURATION
# ============================================================================

MAX_REQUEST_SIZE = 5 * 1024 * 1024   # 5 MB max payload
MAX_CONTEXT_ID_LEN = 200             # Max length for context IDs
MAX_BODY_FIELD_LEN = 10000           # Max length for any single string field
MAX_STORE_ITEMS = 500                # Max items per store category (prevent memory bomb)
RATE_LIMIT_WINDOW = 60               # seconds
RATE_LIMIT_MAX_REQUESTS = 120        # max requests per window per IP
ALLOWED_SCOPES = {"category", "merchant", "customer", "trigger"}

# ============================================================================
# LOGGING (sanitized - no secrets in logs)
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vera-bot")

# ============================================================================
# APP SETUP
# ============================================================================

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_SIZE  # Flask built-in size limit
START_TIME = time.time()

# ============================================================================
# IN-MEMORY DATA STORE
# ============================================================================

store = {
    "categories": {},
    "merchants": {},
    "customers": {},
    "triggers": {},
}

conversations = {}

# ============================================================================
# SECURITY: RATE LIMITER (in-memory, per IP)
# ============================================================================

rate_limit_tracker = defaultdict(list)  # IP -> [timestamp, timestamp, ...]


def is_rate_limited(ip: str) -> bool:
    """Check if an IP has exceeded the rate limit."""
    now = time.time()
    # Clean old entries outside the window
    rate_limit_tracker[ip] = [
        t for t in rate_limit_tracker[ip] if now - t < RATE_LIMIT_WINDOW
    ]
    if len(rate_limit_tracker[ip]) >= RATE_LIMIT_MAX_REQUESTS:
        return True
    rate_limit_tracker[ip].append(now)
    return False


# ============================================================================
# SECURITY: INPUT SANITIZATION
# ============================================================================

# Patterns commonly used in prompt injection attacks
PROMPT_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"disregard\s+(all\s+)?previous",
    r"you\s+are\s+now\s+a",
    r"system\s*:\s*",
    r"<\s*script\s*>",
    r"javascript\s*:",
    r"\{\{.*\}\}",        # Template injection
    r"<\s*iframe",
    r"on(error|load|click)\s*=",
]

COMPILED_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in PROMPT_INJECTION_PATTERNS
]


def contains_injection(text: str) -> bool:
    """Check if text contains common prompt injection patterns."""
    if not isinstance(text, str):
        return False
    for pattern in COMPILED_INJECTION_PATTERNS:
        if pattern.search(text):
            return True
    return False


def sanitize_string(value: str, max_len: int = MAX_BODY_FIELD_LEN) -> str:
    """Sanitize a string value: trim length, strip control characters."""
    if not isinstance(value, str):
        return str(value)[:max_len]
    # Remove null bytes and other dangerous control chars (keep newlines, tabs)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return cleaned[:max_len]


def deep_scan_for_injection(data, path="root") -> list[str]:
    """Recursively scan a dict/list for prompt injection in string values."""
    warnings = []
    if isinstance(data, dict):
        for key, val in data.items():
            warnings.extend(deep_scan_for_injection(val, path=f"{path}.{key}"))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            warnings.extend(deep_scan_for_injection(item, path=f"{path}[{i}]"))
    elif isinstance(data, str):
        if contains_injection(data):
            warnings.append(f"Potential injection at {path}")
    return warnings


def validate_context_id(context_id: str) -> bool:
    """Validate context_id format: alphanumeric, underscores, hyphens, dots only."""
    if not context_id or len(context_id) > MAX_CONTEXT_ID_LEN:
        return False
    return bool(re.match(r"^[a-zA-Z0-9_\-\.]+$", context_id))


# ============================================================================
# SECURITY: MIDDLEWARE
# ============================================================================

@app.before_request
def security_checks():
    """Run security checks before every request."""
    # 1. Rate limiting
    client_ip = request.remote_addr or "unknown"
    if is_rate_limited(client_ip):
        logger.warning(f"Rate limited: {client_ip}")
        return jsonify({"error": "rate_limited", "message": "Too many requests. Try again later."}), 429

    # 2. API token check (if configured)
    if BOT_API_TOKEN:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != BOT_API_TOKEN:
            # Allow healthz without auth (standard practice)
            if request.path != "/v1/healthz":
                logger.warning(f"Unauthorized request from {client_ip} to {request.path}")
                return jsonify({"error": "unauthorized", "message": "Invalid or missing API token."}), 401

    # 3. Block unexpected paths (only our routes)
    allowed_paths = {"/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply"}
    if request.path not in allowed_paths:
        return jsonify({"error": "not_found"}), 404

    # 4. Track request for logging
    g.request_start = time.time()


@app.after_request
def add_security_headers(response):
    """Add security headers to every response."""
    # Prevent browsers from interpreting files as something else
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Prevent clickjacking
    response.headers["X-Frame-Options"] = "DENY"
    # No caching of sensitive responses
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    # Block cross-origin requests
    response.headers["Access-Control-Allow-Origin"] = ""
    # Don't leak server info
    response.headers["Server"] = "vera-bot"
    # Remove default Flask header if present
    response.headers.pop("X-Powered-By", None)

    # Log request duration
    duration = time.time() - g.get("request_start", time.time())
    logger.info(f"{request.method} {request.path} -> {response.status_code} ({duration:.3f}s)")

    return response


@app.errorhandler(Exception)
def handle_error(error):
    """Catch-all error handler: never leak stack traces to the client."""
    # Log the real error internally
    logger.error(f"Unhandled error on {request.path}: {type(error).__name__}: {error}")
    # Return a safe generic message to the client
    return jsonify({"error": "internal_error", "message": "Something went wrong."}), 500


@app.errorhandler(413)
def handle_too_large(error):
    """Reject payloads exceeding MAX_REQUEST_SIZE."""
    return jsonify({"error": "payload_too_large", "message": f"Max payload size is {MAX_REQUEST_SIZE // (1024*1024)} MB."}), 413


@app.errorhandler(404)
def handle_not_found(error):
    return jsonify({"error": "not_found"}), 404


@app.errorhandler(405)
def handle_method_not_allowed(error):
    return jsonify({"error": "method_not_allowed"}), 405


# ============================================================================
# PART 1: BASIC ENDPOINTS
# ============================================================================

@app.route("/v1/healthz", methods=["GET"])
def healthz():
    """Health check - judge calls this to verify bot is alive."""
    uptime = int(time.time() - START_TIME)
    return jsonify({
        "status": "ok",
        "uptime_seconds": uptime,
        "contexts_loaded": {
            "category": len(store["categories"]),
            "merchant": len(store["merchants"]),
            "customer": len(store["customers"]),
            "trigger": len(store["triggers"]),
        }
    })


@app.route("/v1/metadata", methods=["GET"])
def metadata():
    """Return team info - judge reads this once at the start."""
    return jsonify({
        "team_name": "Team Raju",
        "team_members": ["Raju"],
        "model": "gemini-2.5-flash",
        "approach": "Trigger-routed prompt composer with Gemini, auto-reply detection, and intent-transition handling",
        "contact_email": "raju@example.com",
        "version": "1.0.0",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    })


# ============================================================================
# PART 2: CONTEXT INGESTION (with validation)
# ============================================================================

@app.route("/v1/context", methods=["POST"])
def receive_context():
    """
    Judge pushes category/merchant/customer/trigger data here.
    Validates input, checks for injection, then stores with version tracking.
    """
    # --- Validate JSON body ---
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"accepted": False, "reason": "invalid_json"}), 400

    scope = data.get("scope")
    context_id = data.get("context_id")
    version = data.get("version", 1)
    payload = data.get("payload", {})

    # --- Validate required fields ---
    if not scope or not context_id:
        return jsonify({"accepted": False, "reason": "missing_required_fields: scope, context_id"}), 400

    # --- Validate scope ---
    if scope not in ALLOWED_SCOPES:
        return jsonify({"accepted": False, "reason": f"invalid_scope: {scope}"}), 400

    # --- Validate context_id format (prevent path traversal, injection) ---
    if not validate_context_id(context_id):
        return jsonify({"accepted": False, "reason": "invalid_context_id_format"}), 400

    # --- Validate version is a positive integer ---
    if not isinstance(version, int) or version < 1:
        return jsonify({"accepted": False, "reason": "version_must_be_positive_integer"}), 400

    # --- Validate payload is a dict ---
    if not isinstance(payload, dict):
        return jsonify({"accepted": False, "reason": "payload_must_be_object"}), 400

    # --- Scan payload for prompt injection ---
    injection_warnings = deep_scan_for_injection(payload)
    if injection_warnings:
        logger.warning(f"Injection attempt blocked in context {context_id}: {injection_warnings}")
        return jsonify({"accepted": False, "reason": "payload_contains_suspicious_content"}), 400

    # --- Map scope to store key ---
    scope_map = {
        "category": "categories",
        "merchant": "merchants",
        "customer": "customers",
        "trigger": "triggers",
    }
    store_key = scope_map[scope]

    # --- Check store size cap (prevent memory exhaustion) ---
    if context_id not in store[store_key] and len(store[store_key]) >= MAX_STORE_ITEMS:
        return jsonify({"accepted": False, "reason": "store_capacity_reached"}), 507

    # --- Check for stale version (reject strictly older versions) ---
    existing = store[store_key].get(context_id)
    if existing and existing["version"] > version:
        return jsonify({
            "accepted": False,
            "reason": "stale_version",
            "current_version": existing["version"],
        }), 409

    # --- Store the new version ---
    stored_at = datetime.now(timezone.utc).isoformat()
    store[store_key][context_id] = {
        "version": version,
        "payload": payload,
        "stored_at": stored_at,
    }

    logger.info(f"Context stored: scope={scope} id={context_id} v={version}")

    return jsonify({
        "accepted": True,
        "ack_id": f"ack_{context_id}_v{version}",
        "stored_at": stored_at,
    })


# ============================================================================
# SMART FALLBACK - Data-driven messages when Gemini times out
# ============================================================================

def _build_smart_fallback(merchant: dict, category: dict, trigger: dict, customer: dict | None) -> dict:
    """Build a high-compulsion message using actual merchant/category data."""
    identity = merchant.get("identity", {})
    perf = merchant.get("performance", {})
    offers = merchant.get("offers", [])
    merchant_name = identity.get("name", "there")
    locality = identity.get("locality", "your area")
    kind = trigger.get("kind", "update")
    headline = trigger.get("headline", "")
    is_customer = trigger.get("scope") == "customer" and customer is not None

    # Extract real numbers
    ctr = perf.get("ctr", 0)
    peer_ctr = perf.get("peer_median_ctr", 0)
    searches = perf.get("monthly_searches", 0)
    missed = perf.get("missed_searches", 0)
    best_offer = offers[0] if offers else {}
    offer_str = f"{best_offer.get('title', 'service')} @ \u20B9{best_offer.get('price', '')}" if best_offer.get("price") else ""

    # Customer-scoped fallback
    if is_customer and customer:
        cust_name = customer.get("name", "there")
        days_ago = customer.get("last_visit_days_ago", 180)
        body = f"Hi {cust_name}, {merchant_name} here \U0001F44B It's been {days_ago} days since your last visit."
        if offer_str:
            body += f" We have {offer_str} available for you."
        body += " Want me to book a slot for you?"
        return {
            "body": body,
            "cta": "binary_yes_no",
            "send_as": "merchant_on_behalf",
            "suppression_key": trigger.get("suppression_key", f"trig:{kind}"),
            "rationale": f"Customer recall: {days_ago} days lapsed, specific offer cited, single CTA."
        }

    # Research digest
    if kind == "research_digest":
        digest_items = category.get("digest", [])
        if digest_items:
            item = digest_items[0]
            source = item.get("source", "recent study")
            title = item.get("title", headline)
            body = f"Dr., quick flag: {title}. Worth a 2-min read. Want me to pull the abstract + draft a patient-ed WhatsApp you can share? \u2014 {source}"
        else:
            body = f"{merchant_name}, new research relevant to your practice just dropped: {headline}. Want me to pull the abstract for you?"
        return {
            "body": body, "cta": "open_ended", "send_as": "vera",
            "suppression_key": trigger.get("suppression_key", ""),
            "rationale": "Research digest: cited source, offered concrete next step, curiosity lever."
        }

    # Performance dip
    if kind == "perf_dip":
        body = f"{merchant_name}, heads up: {headline}."
        if ctr and peer_ctr:
            body += f" Your CTR is {ctr}% vs {peer_ctr}% {locality} peer median."
        if offer_str:
            body += f" You already have {offer_str} \u2014 want me to draft a quick campaign around it?"
        else:
            body += " Want me to suggest a quick fix?"
        return {
            "body": body, "cta": "binary_yes_no", "send_as": "vera",
            "suppression_key": trigger.get("suppression_key", ""),
            "rationale": f"Perf dip: specific CTR numbers ({ctr}% vs {peer_ctr}%), loss aversion, offer cited."
        }

    # Festival / seasonal
    if kind in ("festival_upcoming", "festival_campaign", "category_seasonal"):
        body = f"{merchant_name}, {headline}."
        if offer_str:
            body += f" Your {offer_str} is perfect for this. Want me to draft a campaign?"
        else:
            body += " Want me to help you plan a timely campaign?"
        return {
            "body": body, "cta": "binary_yes_no", "send_as": "vera",
            "suppression_key": trigger.get("suppression_key", ""),
            "rationale": "Festival trigger: timely, specific offer, single CTA."
        }

    # Default smart fallback - uses whatever data is available
    body = f"Hi {merchant_name}"
    if missed and missed > 0:
        body += f", {missed} people in {locality} searched for your services but couldn't find you this month."
        if offer_str:
            body += f" Should I send them {offer_str}?"
        else:
            body += " Want me to help increase your visibility?"
    elif searches and searches > 0:
        body += f", {searches} people searched for services like yours in {locality} this month."
        if offer_str:
            body += f" Your {offer_str} could convert more of them. Want me to draft a campaign?"
        else:
            body += " Want me to show you how to capture more of them?"
    elif offer_str:
        body += f". Quick update: {headline}. You already have {offer_str} listed \u2014 want me to push it to nearby customers?"
    else:
        body += f". {headline}. Want me to walk you through the next step?"

    return {
        "body": body,
        "cta": "open_ended" if not offer_str else "binary_yes_no",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key", f"trig:{kind}"),
        "rationale": f"Smart fallback for {kind}: used merchant data (searches={searches}, missed={missed}), real offer, locality-specific."
    }


# ============================================================================
# PART 3: TICK - GEMINI-POWERED MESSAGE COMPOSITION
# ============================================================================

@app.route("/v1/tick", methods=["POST"])
def tick():
    """
    Judge calls this with a timestamp and available trigger IDs.
    For each trigger, we look up the merchant + category + customer,
    call Gemini to compose a message, and return the actions.
    """
    # --- Validate JSON body ---
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "invalid_json"}), 400

    now = data.get("now", datetime.now(timezone.utc).isoformat())
    available_triggers = data.get("available_triggers", [])

    if not isinstance(available_triggers, list):
        return jsonify({"error": "available_triggers_must_be_list"}), 400

    # Sanitize trigger IDs
    for tid in available_triggers:
        if not isinstance(tid, str) or not validate_context_id(tid):
            return jsonify({"error": f"invalid_trigger_id: {tid}"}), 400

    actions = []

    for trigger_id in available_triggers:
        # --- Look up trigger ---
        trigger_entry = store["triggers"].get(trigger_id)
        if not trigger_entry:
            logger.warning(f"Trigger not found in store: {trigger_id}")
            continue
        trigger = trigger_entry["payload"]

        # --- Look up merchant ---
        merchant_id = trigger.get("merchant_id")
        if not merchant_id:
            logger.warning(f"Trigger {trigger_id} has no merchant_id")
            continue
        merchant_entry = store["merchants"].get(merchant_id)
        if not merchant_entry:
            logger.warning(f"Merchant not found: {merchant_id}")
            continue
        merchant = merchant_entry["payload"]

        # --- Look up category ---
        category_slug = merchant.get("category_slug", "")
        category_entry = store["categories"].get(category_slug)

        # Fallback: try to infer category from trigger_id or merchant identity
        if not category_entry:
            for cat_key in store["categories"]:
                if cat_key in trigger_id.lower() or cat_key in merchant_id.lower():
                    category_entry = store["categories"][cat_key]
                    category_slug = cat_key
                    break

        # Fallback: try first available category
        if not category_entry and store["categories"]:
            category_slug = next(iter(store["categories"]))
            category_entry = store["categories"][category_slug]
            logger.info(f"Using fallback category: {category_slug}")

        # Final fallback: create minimal category
        if not category_entry:
            logger.warning(f"No category found, using minimal fallback for {trigger_id}")
            category = {"name": "business", "voice": "professional"}
        else:
            category = category_entry["payload"]

        # --- Look up customer (if customer-scoped trigger) ---
        customer = None
        customer_id = trigger.get("customer_id")
        if customer_id:
            customer_entry = store["customers"].get(customer_id)
            if customer_entry:
                customer = customer_entry["payload"]

        # --- Compose message via Gemini ---
        result = compose_message(category, merchant, trigger, customer)
        if not result or not result.get("body"):
            logger.warning(f"Composition returned None for trigger {trigger_id}, using smart fallback")
            result = _build_smart_fallback(merchant, category, trigger, customer)

        # --- Build the action ---
        is_customer_facing = trigger.get("scope") == "customer" and customer is not None
        conv_id = f"conv_{merchant_id}_{trigger.get('kind', 'msg')}_{trigger_id[-4:]}"

        action = {
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": result.get("send_as", "vera"),
            "trigger_id": trigger_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": [],
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": result["suppression_key"],
            "rationale": result["rationale"],
        }

        actions.append(action)

        # --- Store conversation for multi-turn tracking ---
        conversations[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trigger_id,
            "history": [
                {"turn": 1, "from": "vera", "body": result["body"], "at": now}
            ],
            "status": "active",
        }

        logger.info(f"Action composed: trigger={trigger_id} conv={conv_id}")

    return jsonify({"actions": actions})


# ============================================================================
# PART 4: REPLY - MULTI-TURN CONVERSATION HANDLER
# ============================================================================

@app.route("/v1/reply", methods=["POST"])
def reply():
    """
    Judge sends a simulated merchant/customer reply.
    Bot uses multi_turn intelligence to formulate the next step (send, wait, end).
    """
    # --- Validate JSON body ---
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "invalid_json"}), 400

    # --- Validate required fields ---
    conversation_id = data.get("conversation_id")
    message = data.get("message", "")
    merchant_id = data.get("merchant_id")
    turn_number = data.get("turn_number", 2)
    received_at = data.get("received_at", datetime.now(timezone.utc).isoformat())

    if not conversation_id or not isinstance(conversation_id, str):
        return jsonify({"error": "missing_conversation_id"}), 400

    if not message or not isinstance(message, str):
        return jsonify({"error": "missing_message"}), 400

    # --- Sanitize the merchant message ---
    message = sanitize_string(message, max_len=2000)

    # --- Fetch or initialize conversation tracking ---
    conversation = conversations.get(conversation_id, {
        "merchant_id": merchant_id,
        "history": [],
        "status": "active"
    })

    if not merchant_id:
        merchant_id = conversation.get("merchant_id")

    # --- Fetch merchant & category data ---
    merchant_entry = store["merchants"].get(merchant_id, {})
    merchant = merchant_entry.get("payload", {})

    category_slug = merchant.get("category_slug", "")
    category_entry = store["categories"].get(category_slug, {})
    category = category_entry.get("payload", {})

    # --- Execute multi-turn reasoning ---
    result = handle_reply_message(
        conversation=conversation,
        merchant=merchant,
        category=category,
        merchant_message=message,
        turn_number=turn_number,
    )

    # --- Update conversation history ---
    conversation.setdefault("history", [])
    conversation["history"].append({
        "turn": turn_number,
        "from": data.get("from_role", "merchant"),
        "body": message,
        "at": received_at,
    })

    if result.get("action") == "send":
        conversation["history"].append({
            "turn": turn_number,
            "from": "vera",
            "body": result.get("body", ""),
            "at": datetime.now(timezone.utc).isoformat(),
        })
    elif result.get("action") == "end":
        conversation["status"] = "ended"

    conversations[conversation_id] = conversation

    return jsonify(result)


# ============================================================================
# RUN SERVER
# ============================================================================

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  [BOT] Vera Bot Server - magicpin AI Challenge")
    print("=" * 60)

    if not GEMINI_API_KEY or GEMINI_API_KEY == "your_api_key_here":
        print("\n  [!] WARNING: Set your GEMINI_API_KEY in .env file!")
        print("  Open .env and replace 'your_api_key_here' with your key.\n")
    else:
        print(f"\n  [OK] Gemini API Key loaded (ends with ...{GEMINI_API_KEY[-4:]})")
        init_gemini(GEMINI_API_KEY)
        print("  [OK] Gemini model initialized (gemini-2.5-flash)")

    if BOT_API_TOKEN:
        print("  [OK] API token authentication ENABLED")
    else:
        print("  [!!] API token authentication DISABLED (set BOT_API_TOKEN in .env to enable)")

    port = int(os.environ.get("PORT", 5000))
    print(f"  [>>] Server starting on port {port}")
    print(f"  [>>] Max payload: {MAX_REQUEST_SIZE // (1024*1024)} MB | Rate limit: {RATE_LIMIT_MAX_REQUESTS} req/{RATE_LIMIT_WINDOW}s")
    print("=" * 60 + "\n")

    app.run(host="0.0.0.0", port=port, debug=False)
