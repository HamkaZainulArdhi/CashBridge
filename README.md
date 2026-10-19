# CashBridge — SME Finance Copilot

CashBridge is an SME finance copilot that helps small business owners **see cash
shortfalls before they happen**, understand why, and evaluate non-loan options
before being offered a proportional short-term loan. All banking actions are
simulated in a sandbox.

Core principle: **code computes the numbers, the LLM explains them.** The LLM
never invents amounts and never executes sensitive actions without explicit
approval.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# Generate the 3 demo businesses (12 weeks of synthetic data) + demo consent
.venv\Scripts\python scripts\seed_data.py

# Ask the copilot (works offline with MOCK_LLM=1)
.venv\Scripts\python scripts\ask.py --business online "Will I run short on cash?"

# Run the web app
.venv\Scripts\python scripts\run_web.py            # http://127.0.0.1:8000

# Run tests
.venv\Scripts\python -m pytest
```

Copy `.env.example` to `.env` and fill in `OPENROUTER_API_KEY` when you want the
real LLM. With `MOCK_LLM=1` the app runs end-to-end using deterministic
template explanations.

## Demo businesses

| Business | Sector | Role in demo |
|---|---|---|
| Warung Sembako Bu Sari | retail | Healthy control — no gap detected |
| Dapur Nusantara Catering | catering | Gap detected, **loan rejected** (repayment capacity insufficient) |
| Kirana Online Store | ecommerce | Gap detected, **loan approved** (moving-money happy path) |

## Repo layout

```
cashbridge/    deterministic core: db, seed, ingest, forecast, gap, sizing, products, recommend, weekly
app/           FastAPI web app + sandbox bank API: orchestrator (LLM tool calling), policy engine,
               audit hash-chain, guardrails, service layer, tools, Jinja2 templates
scripts/       CLI helpers (seed data, ask the copilot, run the web app)
tests/         pytest suite (analytics, policy, security, web, end-to-end)
docs/          evaluation notes and demo video script
```

## Status (roadmap)

- [x] **Week 1** — repo skeleton, SQLite schema, synthetic data generator, CSV ingest, forecast (3 scenarios), gap detection, loan sizing, alternatives-first recommendations, evaluation helpers
- [x] **Week 2** — policy engine (T0–T3 + amount caps + idempotency + approval gates), tamper-evident audit hash-chain, guardrails (numeric consistency + prompt-injection wrapping), service layer (consent → draft → approve → sandbox disburse → repayments), LLM orchestrator with OpenAI-compatible tool calling (`OPENROUTER_BASE_URL` + `OPENROUTER_API_KEY` from env), `MOCK_LLM` template engine, `scripts/ask.py` CLI
- [x] **Week 3** — FastAPI web app (Jinja2 + Chart.js): first-run onboarding with CSV upload (<3 min to the "when will money run short" answer), dashboard with scenarios chart, alternatives-first options, draft → approve → disburse flow, what-if simulations, weekly delta, bill reminders, re-upload with dedup, onboarding consent + revocable, audit page; sandbox bank API (`/api/bank/...`, idempotent disburse, 409s for replay/no-approval)
- [ ] **Week 4** — evaluation metrics, security tests, demo video

### Verified demo scenarios (seed 42)

| Business | Balance today | 14-day forecast | Decision |
|---|---|---|---|
| Warung Sembako Bu Sari | IDR 48.5M | min low 34.4M — no gap | No loan needed |
| Dapur Nusantara Catering | IDR 12.5M | gap expected, depth 38.6M | **Loan blocked** (capacity −0.18x; collection acceleration clears it) |
| Kirana Online Store | IDR 15.6M | gap risk, depth 12.4M | **Receivable Bridge IDR 13.6M** (coverage 1.37x) |

Forecast band coverage vs. hidden ground truth: 100% / 86% / 93% of the 14
projected days. 65 tests passing.

## Web app

`python scripts/run_web.py` → http://127.0.0.1:8000

The UI follows `design.md` (dark-mode-first, data-dense cockpit): elevation-based
charcoal surfaces, lightened semantic colors (#8AB4F8 / #81C995 / #F28B82),
tabular numerals always at full primary contrast, no shadows on resting cards,
outlined icons, skeletal shimmer loading, and a persistent ask bar.
Fonts: Google Sans (display + body, weights 400–700 + italic) loads from the
Google Fonts CDN with `display=swap`; Roboto, Roboto Mono, and Plus Jakarta Sans
are bundled locally in `app/static/fonts/` as the automatic offline fallback,
so the UI stays intact without internet.

| Page | What it shows |
|---|---|
| `/` | watchlist: every business with balance, 7-day delta chip, outlook, consent |
| `/business/{id}` | 14-day chart (low/expected/high + safety line), non-loan options first, draft → approve → disburse, repayments, this-week delta, bill reminders, CSV re-upload, recent audit — plus the bottom **ask bar** with slide-up grounded answers and what-if simulations in the rail |
| `/onboard` | first run: create a business + upload transactions, answer in under three minutes |
| `/bank` | sandbox lender view: products, loans, ledger, API docs |
| `/audit` | tamper-evident hash-chain trail with integrity check |
| `/api/docs` | interactive OpenAPI docs (includes the `/api/bank/...` sandbox endpoints) |

A **ticker strip** (business, balance, Δ 7 days) sits under the top nav on every page.

> Note: every forecast is an estimate, not financial advice. All disbursements
> and repayments are sandbox simulations. This is not a licensed financial
> advisor.
