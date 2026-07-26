# API cost transparency.
# The Anthropic API does NOT expose an account's credit balance to
# standard (sk-ant-) keys - that lives in the web console. What we CAN
# do honestly: price every call from its returned token usage and keep a
# running total. Prices are USD per million tokens (public price list,
# mid-2026); unknown models show "n/a" rather than a wrong number.
import json
import os

_ATOMIC_SUFFIX = ".tmp"

# longest-prefix match against the model id
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-haiku": (0.25, 1.25),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-3-7-sonnet": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus-4": (15.00, 75.00),
}

BILLING_URL = "https://console.anthropic.com/settings/billing"

# shown when the models endpoint can't be reached; cheapest first
FALLBACK_MODELS = ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"]


def price_for(model):
    """(input_per_mtok, output_per_mtok) or None if unknown."""
    best = None
    for prefix, p in PRICES.items():
        if model.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), p)
    return best[1] if best else None


def price_label(model):
    p = price_for(model)
    if not p:
        return "pricing n/a"
    return f"${p[0]:g}/M in, ${p[1]:g}/M out"


def estimate(model, input_tokens, output_tokens):
    """Dollar cost of one call, or None for unknown models."""
    p = price_for(model)
    if not p:
        return None
    return input_tokens / 1e6 * p[0] + output_tokens / 1e6 * p[1]


def _spend_path():
    return os.path.join("data", "api_spend.json")


def load_spend():
    """{'total_usd': float, 'calls': int} tracked by this app."""
    try:
        with open(_spend_path(), encoding="utf-8") as f:
            d = json.load(f)
        return {"total_usd": float(d.get("total_usd", 0.0)),
                "calls": int(d.get("calls", 0))}
    except Exception:
        return {"total_usd": 0.0, "calls": 0}


def record_spend(cost_usd):
    """Add one call's cost to the persistent running total."""
    d = load_spend()
    d["calls"] += 1
    if cost_usd:
        d["total_usd"] += cost_usd
    os.makedirs("data", exist_ok=True)
    tmp = _spend_path() + _ATOMIC_SUFFIX
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, _spend_path())
    return d


def list_models(api_key):
    """Model ids from the API (newest first), FALLBACK_MODELS on failure."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        ids = [m.id for m in client.models.list(limit=50)]
        return ids or list(FALLBACK_MODELS)
    except Exception:
        return list(FALLBACK_MODELS)
