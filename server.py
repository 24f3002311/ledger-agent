import json
import re
import os
from collections import defaultdict
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify

app = Flask(__name__)

DATA_FILE = os.environ.get("LEDGER_FILE", "ledger.ndjson")
TZ = ZoneInfo("Asia/Kolkata")

RATES = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.09"),
    "INR": Decimal("0.01106"),
}

MONTHS = {
    "january": 1, "february": 2, "march": 3,
    "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}

PRODUCTS = [
    "Rice Cooker", "Air Fryer", "Grinder", "Juicer",
    "Toaster", "Blender", "Mixer", "Kettle",
]

REGIONS = ["North", "South", "East", "West", "Central"]


def load_ledger():
    with open(DATA_FILE, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]

    # Keep the newest version of each order.
    current = {}

    for row in rows:
        oid = row["id"]
        updated = datetime.fromisoformat(row["updated_at"])

        if oid not in current:
            current[oid] = row
        else:
            previous = datetime.fromisoformat(
                current[oid]["updated_at"]
            )
            if updated > previous:
                current[oid] = row

    return list(current.values())


ORDERS = load_ledger()


def money(value):
    return float(
        Decimal(value).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP
        )
    )


def amount_usd(order):
    amount = Decimal(str(order["amount"]))
    currency = order["currency"]

    if currency not in RATES:
        raise ValueError(f"Unknown currency: {currency}")

    return amount * RATES[currency]


def order_date(order):
    return datetime.fromisoformat(
        order["created_at"]
    ).astimezone(TZ).date()


def parse_date_range(question):
    """Return an inclusive date range when a month/year is specified."""
    q = question.lower()

    # Explicit YYYY-MM-DD dates.
    found = re.findall(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", q)

    if len(found) >= 2:
        start = date(*map(int, found[0]))
        end = date(*map(int, found[1]))
        return start, end

    if len(found) == 1:
        start = date(*map(int, found[0]))
        return start, start

    # A month name and year, e.g. March 2026.
    for name, month in MONTHS.items():
        match = re.search(
            rf"\b{name}\s+(20\d{{2}})\b|\b(20\d{{2}})\s+{name}\b",
            q
        )

        if match:
            year = int(match.group(1) or match.group(2))

            if match.group(1):
                actual_month = month
            else:
                actual_month = month

            if actual_month == 12:
                end = date(year + 1, 1, 1)
            else:
                end = date(year, actual_month + 1, 1)

            return date(year, actual_month, 1), date.fromordinal(
                end.toordinal() - 1
            )

    # Numeric year-month, e.g. 2026-03.
    match = re.search(r"\b(20\d{2})-(\d{2})\b", q)

    if match:
        year, month = map(int, match.groups())

        if 1 <= month <= 12:
            if month == 12:
                end = date(year + 1, 1, 1)
            else:
                end = date(year, month + 1, 1)

            return date(year, month, 1), date.fromordinal(
                end.toordinal() - 1
            )

    return None, None


def filter_orders(question):
    q = question.lower()
    start, end = parse_date_range(question)

    selected = ORDERS

    # Identify a region.
    for region in REGIONS:
        if re.search(rf"\b{region.lower()}\b", q):
            selected = [
                o for o in selected
                if o["region"].lower() == region.lower()
            ]
            break

    # Identify a product.
    for product in PRODUCTS:
        if product.lower() in q:
            selected = [
                o for o in selected
                if o["product"].lower() == product.lower()
            ]
            break

    # Identify a customer ID.
    match = re.search(r"\bc\d{3,}\b", q, re.I)

    if match:
        customer = match.group().upper()
        selected = [
            o for o in selected
            if o["customer"].upper() == customer
        ]

    # Apply the business date range.
    if start and end:
        selected = [
            o for o in selected
            if start <= order_date(o) <= end
        ]

    return selected


def answer_question(question):
    q = question.lower().strip()
    selected = filter_orders(question)

    # Recognize the requested metric.
    is_refund = any(
        word in q for word in ("refund", "refunded", "reimburse")
    )

    is_revenue = any(
        word in q for word in (
            "revenue", "sales", "earned", "earnings", "turnover",
            "spend", "spent", "spending", "purchase value"
        )
    )

    is_paid_query = any(
        word in q for word in ("paid", "completed")
    )

    is_count = any(
        phrase in q for phrase in (
            "how many", "number of", "count of", "count the"
        )
    )

    is_units = any(
        word in q for word in ("units", "quantity", "items sold")
    )

    # Refunds and revenue have different status filters.
    if is_refund:
        relevant = [o for o in selected if o["status"] == "refunded"]
    elif is_revenue or is_units or is_paid_query:
        relevant = [o for o in selected if o["status"] == "paid"]
    else:
        relevant = selected

    # Ranking questions.
    if any(word in q for word in ("top", "highest", "most", "best")):
        if "product" in q:
            grouped = defaultdict(Decimal)

            for o in relevant:
                grouped[o["product"]] += amount_usd(o)

            if grouped:
                return max(grouped, key=grouped.get)

        if "customer" in q:
            grouped = defaultdict(Decimal)

            for o in relevant:
                grouped[o["customer"]] += amount_usd(o)

            if grouped:
                return max(grouped, key=grouped.get)

    # Order counts.
    if is_count:
        if "customer" in q and "order" not in q:
            return len({o["customer"] for o in relevant})

        if "unit" in q:
            return sum(int(o["qty"]) for o in relevant)

        return len(relevant)

    # Units sold.
    if is_units:
        return sum(int(o["qty"]) for o in relevant)

    # Average order value.
    if "average" in q or "avg" in q:
        if not relevant:
            return 0.0

        total = sum(
            (amount_usd(o) for o in relevant),
            Decimal("0")
        )

        return money(total / len(relevant))

    # Total money.
    total = sum(
        (amount_usd(o) for o in relevant),
        Decimal("0")
    )

    return money(total)


@app.route("/", methods=["POST"])
def handle_question():
    body = request.get_json(silent=True)

    if not isinstance(body, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    question = body.get("question")

    if not isinstance(question, str) or not question.strip():
        return jsonify({"error": "A question is required"}), 400

    try:
        answer = answer_question(question)
        return jsonify({"answer": answer})
    except Exception:
        app.logger.exception("Question processing failed")
        return jsonify({"error": "Could not process question"}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "orders_loaded": len(ORDERS),
    })


if __name__ == "__main__":
    print(f"Loaded {len(ORDERS)} current orders")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
