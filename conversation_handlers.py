"""
Multi-turn Conversation Handler Interface
Conforms to Challenge Brief §7.4
"""

from multi_turn import handle_reply_message


def respond(state: dict, merchant_message: str) -> dict:
    """
    Given the conversation state dict + merchant's latest message, produce the reply action.
    Returns: {"action": "send"|"wait"|"end", "body": str, "cta": str, "rationale": str, ...}
    """
    merchant_id = state.get("merchant_id", "unknown")
    history = state.get("history", [])
    turn_number = len(history) + 1
    
    return handle_reply_message(
        conv_state=state,
        merchant_id=merchant_id,
        merchant_message=merchant_message,
        turn_number=turn_number,
        merchant_data=state.get("merchant_data")
    )
