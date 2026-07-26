# RAG: retrieve → build a cited context block → ask Claude. The API key
# comes from settings (user-supplied); without it the caller should keep
# the Ask AI tab disabled.
import anthropic

from . import search, secrets

SYSTEM = (
    "You are HyperSpin Clinic, an assistant for diagnosing and maintaining "
    "a HyperSpin arcade theme collection. Answer from the provided context "
    "chunks when possible and cite them as [n]. The context includes a "
    "conversion knowledge base (4:3 to 16:9 theme conversion rules, video "
    "recorder behavior) and per-theme facts extracted from theme zips. "
    "If the context does not contain the answer, say so plainly and answer "
    "from general knowledge, clearly separated. Be concise and concrete."
)


def overview(cfg):
    """Live one-line collection overview injected into every question so
    the model knows what systems exist even before anything is indexed."""
    try:
        from . import hyperspin_db as hdb
        systems = hdb.list_systems(cfg.get("hyperspin_root", ""))
        if systems:
            return ("Installed systems (from Databases\\Main\\Main.xml): "
                    + ", ".join(systems))
    except Exception:
        pass
    return ""


def ask(cfg, question, k=8, history=None):
    """Returns (answer_text, retrieved_chunks, es_ok, cost_usd|None)."""
    chunks, es_ok = search.hybrid(cfg, question, k=k)
    ctx_lines = []
    ov = overview(cfg)
    if ov:
        ctx_lines.append(f"[live] {ov}")
    for i, c in enumerate(chunks, 1):
        src = c["meta"].get("source", "?")
        ctx_lines.append(f"[{i}] (source: {src})\n{c['text']}")
    context = "\n\n---\n\n".join(ctx_lines) if ctx_lines else "(no indexed context found)"

    messages = list(history or [])
    messages.append({
        "role": "user",
        "content": f"Context chunks:\n\n{context}\n\nQuestion: {question}",
    })
    key = secrets.load()
    if not key:
        raise RuntimeError("No Claude API key stored (Setup tab).")
    client = anthropic.Anthropic(api_key=key)
    model = cfg.get("model", "claude-haiku-4-5")
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        system=SYSTEM,
        messages=messages,
    )
    answer = "".join(b.text for b in resp.content if b.type == "text")
    cost = None
    try:
        from . import costs
        u = resp.usage
        cost = costs.estimate(model, u.input_tokens, u.output_tokens)
        costs.record_spend(cost)
    except Exception:
        pass
    return answer, chunks, es_ok, cost
