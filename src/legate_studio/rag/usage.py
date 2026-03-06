"""
Usage Tracking & Credit Cap Enforcement

Tracks token usage for Managed-tier users and enforces the monthly credit cap.
BYOK users are not tracked here — they pay their provider directly.

Tables used (created by database.py init_db):
  - usage_events: per-request detail rows
  - usage_meters: per-user per-period aggregate meters (upserted)
  - credit_topups: purchased top-up credits for managed users (created here if missing)

Credit model:
  - Managed tier base: $4.50/month (4,500,000 microdollars) included in $5/mo subscription
  - Top-ups: $5 purchase → +$4.50 in token credits (+4,500,000 microdollars)
  - Billing period: calendar month (YYYY-MM)
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Base monthly credit cap for Managed tier: $4.50
BASE_CAP_MICRODOLLARS = 4_500_000

# Each $5 top-up purchase adds this many token microdollars
TOPUP_CREDIT_MICRODOLLARS = 4_500_000

# Approximate cost in microdollars per token (per million tokens)
# microdollars_per_token = dollars_per_million / 1_000_000 * 1_000_000 = dollars_per_million
# So input/output values here ARE microdollars-per-token already.
# e.g. $3/MTok input = 3_000_000 microdollars / 1_000_000 tokens = 3 microdollars/token
COST_TABLE: dict[str, dict[str, float]] = {
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},     # $3/$15 per MTok
    "claude-3-5-sonnet": {"input": 3.0, "output": 15.0},
    "claude-opus-4": {"input": 15.0, "output": 75.0},      # $15/$75 per MTok
    "claude-3-opus": {"input": 15.0, "output": 75.0},
    "claude-3-5-haiku": {"input": 0.8, "output": 4.0},     # $0.80/$4 per MTok
    "claude-3-haiku": {"input": 0.25, "output": 1.25},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},         # $0.15/$0.60 per MTok
    "gpt-4o": {"input": 2.5, "output": 10.0},              # $2.5/$10 per MTok
    "gpt-4-turbo": {"input": 10.0, "output": 30.0},
    "gpt-3.5-turbo": {"input": 0.5, "output": 1.5},
    "gemini-2.0-flash": {"input": 0.075, "output": 0.3},   # $0.075/$0.30 per MTok
    "gemini-2.5-flash": {"input": 0.075, "output": 0.3},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},     # $1.25/$10 per MTok
    "gemini-1.5-pro": {"input": 1.25, "output": 5.0},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.3},
}

# Default fallback rates when no prefix match is found (conservative estimate)
_DEFAULT_RATES = {"input": 3.0, "output": 15.0}


def estimate_cost(provider: str, model: str, tokens_in: int, tokens_out: int) -> int:
    """Estimate cost in microdollars using prefix matching against COST_TABLE.

    Args:
        provider: 'anthropic', 'openai', or 'gemini' (unused currently, model prefix sufficient)
        model: Model ID string (e.g. 'claude-sonnet-4-20250514', 'gpt-4o-mini')
        tokens_in: Number of input/prompt tokens
        tokens_out: Number of output/completion tokens

    Returns:
        Estimated cost in microdollars (int). 0 if tokens are 0.
    """
    if not tokens_in and not tokens_out:
        return 0

    model_lower = model.lower()
    rates = _DEFAULT_RATES

    # Longest prefix match wins
    best_match_len = 0
    for key, r in COST_TABLE.items():
        if model_lower.startswith(key) and len(key) > best_match_len:
            rates = r
            best_match_len = len(key)

    # microdollars = tokens * (dollars_per_MTok / 1_000_000) * 1_000_000
    #              = tokens * dollars_per_MTok
    cost = (tokens_in * rates["input"] + tokens_out * rates["output"])
    return int(cost)


def _get_period() -> str:
    """Get current billing period string (YYYY-MM)."""
    return datetime.utcnow().strftime("%Y-%m")


def _ensure_topup_table(conn) -> None:
    """Ensure credit_topups table exists (created alongside usage tables)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS credit_topups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            period TEXT NOT NULL,
            topup_microdollars INTEGER NOT NULL DEFAULT 4500000,
            purchase_amount_cents INTEGER NOT NULL DEFAULT 500,
            stripe_payment_intent_id TEXT,
            status TEXT NOT NULL DEFAULT 'completed',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def record_usage_event(
    user_id: str,
    provider: str,
    tokens_in: int,
    tokens_out: int,
    cost_microdollars: int,
    event_type: str = "chat",
) -> None:
    """Write a detailed row to usage_events table.

    Args:
        user_id: User's ID
        provider: LLM provider name ('anthropic', 'openai', 'gemini')
        tokens_in: Input/prompt tokens consumed
        tokens_out: Output/completion tokens consumed
        cost_microdollars: Estimated cost in microdollars
        event_type: Type of event (default 'chat')
    """
    from .database import init_db

    try:
        db = init_db()
        db.execute(
            """
            INSERT INTO usage_events
                (user_id, event_type, provider, tokens_in, tokens_out, cost_microdollars)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, event_type, provider, tokens_in, tokens_out, cost_microdollars),
        )
        db.commit()
    except Exception as e:
        logger.error(f"Failed to record usage event for {user_id}: {e}")


def update_usage_meter(
    user_id: str,
    tokens_in: int,
    tokens_out: int,
    cost_microdollars: int,
) -> None:
    """Increment the current month's usage_meters rows (upsert pattern).

    Writes three meter rows per period:
      - chat_tokens_in
      - chat_tokens_out
      - chat_cost_microdollars

    Args:
        user_id: User's ID
        tokens_in: Input/prompt tokens to add
        tokens_out: Output/completion tokens to add
        cost_microdollars: Cost in microdollars to add
    """
    from .database import init_db

    period = _get_period()

    try:
        db = init_db()
        for meter_type, quantity in (
            ("chat_tokens_in", tokens_in),
            ("chat_tokens_out", tokens_out),
            ("chat_cost_microdollars", cost_microdollars),
        ):
            db.execute(
                """
                INSERT INTO usage_meters (user_id, period, meter_type, quantity)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, period, meter_type)
                DO UPDATE SET
                    quantity = quantity + excluded.quantity,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, period, meter_type, quantity),
            )
        db.commit()
    except Exception as e:
        logger.error(f"Failed to update usage meter for {user_id}: {e}")


def get_monthly_usage(user_id: str) -> dict:
    """Get current month's aggregated usage from usage_meters.

    Returns:
        Dict with keys: tokens_in, tokens_out, cost_microdollars (all int)
    """
    from .database import init_db

    period = _get_period()

    try:
        db = init_db()
        rows = db.execute(
            """
            SELECT meter_type, quantity
            FROM usage_meters
            WHERE user_id = ? AND period = ?
            """,
            (user_id, period),
        ).fetchall()

        result = {"tokens_in": 0, "tokens_out": 0, "cost_microdollars": 0}
        for row in rows:
            if row["meter_type"] == "chat_tokens_in":
                result["tokens_in"] = row["quantity"]
            elif row["meter_type"] == "chat_tokens_out":
                result["tokens_out"] = row["quantity"]
            elif row["meter_type"] == "chat_cost_microdollars":
                result["cost_microdollars"] = row["quantity"]
        return result
    except Exception as e:
        logger.error(f"Failed to get monthly usage for {user_id}: {e}")
        return {"tokens_in": 0, "tokens_out": 0, "cost_microdollars": 0}


def get_purchased_topup_credits(user_id: str) -> int:
    """Get total purchased top-up microdollars for the current billing period.

    Args:
        user_id: User's ID

    Returns:
        Total top-up credits in microdollars for this calendar month
    """
    from .database import init_db

    period = _get_period()

    try:
        db = init_db()
        _ensure_topup_table(db)
        row = db.execute(
            """
            SELECT COALESCE(SUM(topup_microdollars), 0) as total
            FROM credit_topups
            WHERE user_id = ? AND period = ? AND status = 'completed'
            """,
            (user_id, period),
        ).fetchone()
        return int(row["total"]) if row else 0
    except Exception as e:
        logger.error(f"Failed to get topup credits for {user_id}: {e}")
        return 0


def check_credit_cap(
    user_id: str,
    cap_microdollars: int = BASE_CAP_MICRODOLLARS,
) -> tuple[bool, int]:
    """Check if user is under their effective monthly credit cap.

    The effective cap = base_cap + purchased top-up credits for this period.

    Args:
        user_id: User's ID
        cap_microdollars: Base monthly cap (default $4.50 = 4_500_000)

    Returns:
        Tuple of (allowed: bool, remaining_microdollars: int)
          - allowed: True if user has remaining credits
          - remaining_microdollars: How many microdollars remain (may be 0 or negative)
    """
    usage = get_monthly_usage(user_id)
    topup_credits = get_purchased_topup_credits(user_id)

    effective_cap = cap_microdollars + topup_credits
    spent = usage["cost_microdollars"]
    remaining = effective_cap - spent

    return (remaining > 0, max(0, remaining))


def record_credit_topup(
    user_id: str,
    stripe_payment_intent_id: str | None = None,
    topup_microdollars: int = TOPUP_CREDIT_MICRODOLLARS,
    purchase_amount_cents: int = 500,
) -> bool:
    """Record a credit top-up purchase for a user.

    This is the accounting side of a top-up. The Stripe payment flow is handled
    separately — this function just records the completed purchase.

    Args:
        user_id: User's ID
        stripe_payment_intent_id: Stripe PaymentIntent ID (for audit trail)
        topup_microdollars: Token credits added (default: 4_500_000 = $4.50)
        purchase_amount_cents: Amount paid in cents (default: 500 = $5.00)

    Returns:
        True if recorded successfully, False on error
    """
    from .database import init_db

    period = _get_period()

    try:
        db = init_db()
        _ensure_topup_table(db)
        db.execute(
            """
            INSERT INTO credit_topups
                (user_id, period, topup_microdollars, purchase_amount_cents,
                 stripe_payment_intent_id, status)
            VALUES (?, ?, ?, ?, ?, 'completed')
            """,
            (user_id, period, topup_microdollars, purchase_amount_cents, stripe_payment_intent_id),
        )
        db.commit()
        logger.info(
            f"Recorded credit topup for {user_id}: "
            f"+{topup_microdollars}µ$ (paid {purchase_amount_cents}¢)"
        )
        return True
    except Exception as e:
        logger.error(f"Failed to record credit topup for {user_id}: {e}")
        return False


def get_usage_summary(user_id: str) -> dict:
    """Get a full usage summary for the current period, suitable for API responses.

    Returns:
        Dict with:
          - tokens_in: int
          - tokens_out: int
          - cost_microdollars: int
          - cost_dollars: float  (rounded to 4 decimal places)
          - base_cap_microdollars: int
          - topup_credits_microdollars: int
          - effective_cap_microdollars: int
          - remaining_microdollars: int
          - remaining_dollars: float
          - cap_dollars: float
          - period: str (YYYY-MM)
          - percent_used: float (0–100+)
    """
    usage = get_monthly_usage(user_id)
    topup = get_purchased_topup_credits(user_id)
    effective_cap = BASE_CAP_MICRODOLLARS + topup
    spent = usage["cost_microdollars"]
    remaining = max(0, effective_cap - spent)
    percent = (spent / effective_cap * 100) if effective_cap > 0 else 0

    return {
        "tokens_in": usage["tokens_in"],
        "tokens_out": usage["tokens_out"],
        "cost_microdollars": spent,
        "cost_dollars": round(spent / 1_000_000, 4),
        "base_cap_microdollars": BASE_CAP_MICRODOLLARS,
        "topup_credits_microdollars": topup,
        "effective_cap_microdollars": effective_cap,
        "remaining_microdollars": remaining,
        "remaining_dollars": round(remaining / 1_000_000, 4),
        "cap_dollars": round(effective_cap / 1_000_000, 4),
        "period": _get_period(),
        "percent_used": round(percent, 1),
    }
