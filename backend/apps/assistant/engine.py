"""Assistant engine (Phase 7): answers ONLY from compiled facts.

The deterministic matcher runs always (tests, offline, no API key).
Matches map question intents onto fact keys — the answer text quotes the
computed numbers verbatim; unknown questions get a capability list with
NO figures at all (a wrong number would violate the no-invention rule).

If ASSISTANT_API_KEY is configured, the compiled facts are handed to an
LLM with a strict system prompt after the deterministic matcher fails;
any LLM failure falls back silently. The deterministic path never calls
the network.
"""
from decimal import Decimal

from apps.assistant.facts import build_business_facts, build_personal_facts

CAPABILITIES = (
    "I can answer from your finance records only — try asking about: "
    "net worth, account balances, this month's income/expense/savings, "
    "biggest expense category, savings goals, loan EMI status, customer "
    "dues, cash/wallet balances, sales and profit this month, low stock, "
    "upcoming recurring transactions. I never invent figures."
)


class AssistantError(Exception):
    pass


def _taka(amount: str) -> str:
    val = Decimal(str(amount)).quantize(Decimal("0.01"))
    return f"৳{val:,}"


def _business_intent(question: str, facts: dict):
    q = question.lower()

    if any(w in q for w in ("due", "owe ", "owes", "owing", "receivab")):
        lines = [f"Total customer due is {_taka(facts['total_customer_due'])}"
                 " as of " + facts["as_of"] + "."]
        if facts["top_customer_dues"]:
            lines.append("Top dues:")
            for d in facts["top_customer_dues"][:5]:
                lines.append(
                    f"• {d['customer']} (via {d['partner']}): "
                    + _taka(d["due"]))
        else:
            lines.append("No customer owes anything right now.")
        return "BUSINESS_DUES", facts["total_customer_due"], lines

    if any(w in q for w in ("cash", "bank", "wallet", "balance")):
        lines = ["Wallet balances from the double-entry ledger as of "
                 + facts["as_of"] + ":",
                 f"• Cash: {_taka(facts['wallets']['cash'])}",
                 f"• Bank & mobile wallets: "
                 f"{_taka(facts['wallets']['bank_and_mobile_wallets'])}",
                 f"Total money-in-hand: {_taka(facts['wallet_total'])}."]
        return "BUSINESS_WALLETS", facts["wallet_total"], lines

    if any(w in q for w in ("profit", "revenue", "sales", "expense",
                            "income", "p&l", "p and l")):
        lines = [f"This month ({facts['as_of']}): revenue "
                 + _taka(facts["month_revenue"])
                 + ", expenses " + _taka(facts["month_expenses"])
                 + ", net profit " + _taka(facts["month_net_profit"]) + "."]
        return "BUSINESS_PNL", facts["month_net_profit"], lines

    if any(w in q for w in ("stock", "inventory", "low stock", "product")):
        if facts["low_stock"]:
            lines = ["Low-stock products (at/below threshold):"]
            for p in facts["low_stock"]:
                lines.append(f"• {p['product']}: {p['stock']} "
                             f"(threshold {p['threshold']})")
        else:
            lines = ["No product is at or below its low-stock threshold."]
        return "BUSINESS_STOCK", None, lines

    if any(w in q for w in ("recurring", "auto", "scheduled")):
        if facts["recurring_due_next_7_days"]:
            lines = ["Recurring rules due within 7 days:"]
            for r in facts["recurring_due_next_7_days"]:
                lines.append(f"• {r['title']}: {_taka(r['amount'])} "
                             f"on {r['next_date']}")
        else:
            lines = ["No recurring rule is due within 7 days."]
        return "BUSINESS_RECURRING", None, lines

    return None


def _personal_intent(question: str, facts: dict):
    q = question.lower()

    if "net worth" in q or "how much am i worth" in q:
        return ("PERSONAL_NET_WORTH", facts["net_worth"],
                [f"Your net worth is {_taka(facts['net_worth'])} "
                 f"(computed from account balances, GIVEN receivables and "
                 f"TAKEN payables) as of {facts['as_of']}."])

    if "balance" in q or "account" in q:
        if facts["accounts"]:
            lines = ["Account balances (ledger-derived):"]
            for a in facts["accounts"]:
                lines.append(f"• {a['account']} ({a['kind']}): "
                             + _taka(a["balance"]))
        else:
            lines = ["You have no active personal accounts yet — create one "
                     "under /api/personal/accounts/."]
        return "PERSONAL_BALANCES", None, lines

    if any(w in q for w in ("spend", "spent", "expense", "income", "saving "
                            "this month", "budget")):
        lines = [f"This month: income {_taka(facts['month_total_income'])}, "
                 f"expense {_taka(facts['month_total_expense'])}, "
                 f"net saved {_taka(facts['month_net_saved'])}."]
        if facts["biggest_expense_category"]:
            lines.append("Biggest expense category: "
                         f"{facts['biggest_expense_category']['category']} "
                         + _taka(facts["biggest_expense_category"]["amount"])
                         + ".")
        return ("PERSONAL_MONTH", facts["month_net_saved"], lines)

    if any(w in q for w in ("goal", "saving target", "hajj", "fund")):
        if facts["savings_goals"]:
            lines = ["Your savings goals:"]
            for g in facts["savings_goals"]:
                lines.append(f"• {g['goal']}: {_taka(g['saved'])} of "
                             + _taka(g["target"]) + f" ({g['status']})")
        else:
            lines = ["You have no savings goals yet."]
        return "PERSONAL_GOALS", None, lines

    if any(w in q for w in ("loan", "emi", "borrow", "lend", "payable")):
        if facts["active_loans"]:
            lines = ["Your active loans:"]
            for l in facts["active_loans"]:
                tail = (f" — {len(l['overdue_installments'])} overdue "
                        f"installment(s)" if l["overdue_installments"] else "")
                lines.append(f"• {l['loan']} ({l['direction']}): remaining "
                             + _taka(l["remaining"]) + tail)
        else:
            lines = ["You have no active loans."]
        return "PERSONAL_LOANS", None, lines

    return None


def answer_question(user, question: str, *, facts: dict,
                    scope: str) -> dict:
    """Deterministic matcher; optional LLM only if a key is configured."""
    lines = None
    intent = None
    headline = None
    if scope == "BUSINESS":
        intent, headline, lines = _business_intent(question, facts) or (
            None, None, None)
    else:
        intent, headline, lines = _personal_intent(question, facts) or (
            None, None, None)

    used_keys = sorted([k for k in facts if not k.startswith("scope")])

    if lines is None:
        # Optional enhanced path, strictly grounded in the same facts.
        llm_answer = _llm_answer_if_configured(user, question, facts)
        if llm_answer:
            return {"answer": llm_answer, "intent": "LLM_GROUNDED",
                    "facts_used": used_keys,
                    "data_basis": ["compiled from your authorized records "
                                   "only"], "scope": scope}
        return {"answer": CAPABILITIES, "intent": "FALLBACK",
                "facts_used": [], "scope": scope,
                "data_basis": ["no figures produced — question not "
                               "recognized"]}

    basis = (f"Computed live from {facts['business_name']} ledger rows "
             f"scoped to your role." if scope == "BUSINESS"
             else f"Computed live from your personal ledger rows.")
    return {"answer": "\n".join(lines), "intent": intent,
            "facts_used": used_keys, "scope": scope,
            "data_basis": [basis]}


def _llm_answer_if_configured(user, question: str, facts: dict) -> str | None:
    from django.conf import settings
    key = getattr(settings, "ASSISTANT_API_KEY", "")
    if not key:
        return None
    try:
        import json as _json
        from urllib import request as _urlreq

        body = _json.dumps({
            "model": getattr(settings, "ASSISTANT_MODEL", "gpt-4o-mini"),
            "messages": [
                {"role": "system",
                 "content": "You answer personal-finance questions for a "
                            "user STRICTLY from the JSON facts you are "
                            "given. If the answer is not in the facts, say "
                            "you cannot find it in their records. Never "
                            "invent or estimate numbers."},
                {"role": "user",
                 "content": "Facts:\n" + _json.dumps(facts)
                            + "\n\nQuestion: " + question},
            ],
            "temperature": 0,
        }).encode()
        req = _urlreq.Request(
            "https://api.openai.com/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"})
        with _urlreq.urlopen(req, timeout=20) as resp:
            payload = _json.loads(resp.read().decode())
        return payload["choices"][0]["message"]["content"]
    except Exception:
        return None  # any failure silently falls back to deterministic
