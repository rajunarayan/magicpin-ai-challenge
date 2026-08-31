# magicpin AI Challenge — Vera Bot Submission

**Team Name**: Team Raju  
**Model Architecture**: Google Gemini Flash via REST API with automated fallback routing  
**Date**: August 2026  

---

## 1. Approach & Architecture

Our solution builds an intelligent, high-conversion WhatsApp merchant assistant ("Vera") by combining a hardened web server with context-routed prompt engineering and robust multi-turn state management.

```
┌─────────────────────────────────────────────────────────────┐
│                    Flask Bot Server (bot.py)                │
│  - 10-layer Security & Rate Limiting                       │
│  - /v1/healthz, /v1/metadata, /v1/context, /v1/tick, /reply │
└──────────────┬───────────────────────────────┬──────────────┘
               │                               │
       POST /v1/tick                   POST /v1/reply
               ▼                               ▼
┌─────────────────────────────┐ ┌─────────────────────────────┐
│    Composer (composer.py)   │ │    Multi-Turn (multi_turn)  │
│  - 4-Context Aggregator     │ │  - Auto-reply detector      │
│  - Trigger Strategy Router  │ │  - Immediate intent switch  │
│  - Compulsion Levers Engine │ │  - Scope guard & opt-out    │
│  - Gemini REST + Fallbacks  │ │  - Conversational context   │
└─────────────────────────────┘ └─────────────────────────────┘
```

### Key Pillars:
1. **Four-Layer Context Ingestion**: Combines `CategoryContext`, `MerchantContext`, `TriggerContext`, and `CustomerContext` into structured, zero-hallucination prompts.
2. **Strategy & Compulsion Levers**: Tailored strategies per trigger kind (research digests, performance dips, seasonal demand, inventory recalls, etc.) enforcing real numbers, peer citations, and single binary CTAs.
3. **Multi-Turn WhatsApp Engine**: Detects canned WhatsApp Business auto-replies to prevent bot loops, handles intent transitions into immediate action, gracefully exits on opt-out, and redirects out-of-scope queries (e.g. GST/tax) back to growth campaigns.
4. **Production Security**: 10 distinct security safeguards including rate limiting, input validation, prompt injection scanning, path traversal blocking, request size caps, and error masking.

---

## 2. Tradeoffs & Decisions

* **Direct REST API vs Heavy SDK**: We adopted a direct REST call mechanism with multi-model fallback (`gemini-3-flash-preview` / `gemini-3.5-flash` / `gemini-3.7-flash`), eliminating library dependency conflicts and improving latency to <2.5s per turn.
* **Heuristics + AI for Multi-Turn**: Rather than relying purely on LLMs for conversation termination, we added deterministic regex filters for immediate opt-outs and auto-reply loops, keeping turn costs and latency minimal.
* **Code-Mixed Language Support**: The model is instructed to adapt to the merchant's exact language preference (e.g., natural Hindi-English code-mix) while maintaining professional peer register.

---

## 3. Deliverables

* **`bot.py`**: Standalone composition function `compose()` + Flask API server.
* **`composer.py`**: Prompt engineering and Gemini REST composition engine.
* **`multi_turn.py`**: Multi-turn conversation logic and auto-reply handler.
* **`submission.jsonl`**: 30 canonical test case outputs.
* **`requirements.txt`**: Python dependencies.
