"""Ask CashBridge a question from the CLI.

Works with MOCK_LLM=1 (deterministic template engine, no network) or with a
real OpenAI-compatible endpoint configured in .env.

Usage:
    python scripts/ask.py --business online "Will I run short on cash?"
    python scripts/ask.py --business katering --tier T0 "Give me advice"
    python scripts/ask.py --engine llm --json --business online "..."
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import orchestrator
from cashbridge import db

DEFAULT_QUESTION = "Will I run short on cash in the next two weeks, and what should I do?"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="*", default=None,
                        help=f"Owner question (default: {DEFAULT_QUESTION!r})")
    parser.add_argument("--db", default=None, help="SQLite path (default: CASHBRIDGE_DB)")
    parser.add_argument("--business", default="online",
                        help="Business id, sector, or name fragment")
    parser.add_argument("--tier", default="T2", choices=["T0", "T1", "T2", "T3"])
    parser.add_argument("--engine", default="auto", choices=["auto", "mock", "llm"])
    parser.add_argument("--json", action="store_true", help="Print the raw result JSON")
    args = parser.parse_args(argv)

    question = " ".join(args.question) if args.question else DEFAULT_QUESTION
    force_engine = None if args.engine == "auto" else args.engine

    conn = db.connect(args.db)
    db.init_db(conn)
    business = db.get_business(conn, args.business)
    if business is None:
        # demo-friendly aliases for the seed keys
        alias = {"katering": "catering"}.get(args.business)
        if alias:
            business = db.get_business(conn, alias)
    if business is None:
        names = ", ".join(b["name"] for b in db.list_businesses(conn)) or \
            "(empty database — run scripts/seed_data.py first)"
        print(f"Unknown business: {args.business!r}. Businesses: {names}", file=sys.stderr)
        return 2

    result = orchestrator.answer_question(
        conn, business["id"], question,
        granted_tier=args.tier, actor="owner", force_engine=force_engine)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["validation"]["ok"] else 1

    answer = result["answer"]
    print(f"engine: {result['engine']} | tier: {args.tier} | "
          f"grounded: {result['validation']['ok']}")
    if result.get("note"):
        print(f"note: {result['note']}")
    print(f"\n{answer['summary']}\n\n{answer['why']}")
    if answer.get("next_steps"):
        print("\nNext steps:")
        for i, step in enumerate(answer["next_steps"], 1):
            print(f"  {i}. {step}")
    if answer.get("key_figures"):
        print("\nKey figures:")
        for figure in answer["key_figures"]:
            print(f"  - {figure['label']}: {figure['value']:,.0f} ({figure['source']})")
    print("\nTools called: " + ", ".join(t["name"] for t in result["tool_calls"]))
    if result["validation"]["issues"]:
        print("Validation issues:")
        for issue in result["validation"]["issues"]:
            print(f"  ! {issue}")
    return 0 if result["validation"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
