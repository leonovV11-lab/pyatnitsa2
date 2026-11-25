"""Summarize offline prerequisites for candidate review without exposing account data."""

import math
from collections import defaultdict

from live_evidence import (
    RECORDED_PROVENANCE,
    assess_recorded_history,
    read_recorded_history,
)
from live_portfolio import read_portfolio
from strategies import BASELINE


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _sizing_blocker(options):
    values = options.budget, options.fee_rate, options.risk_cap
    if any(value is None for value in values):
        return "sizing_missing"
    if not (
        all(_finite_number(value) for value in values)
        and 0 < options.risk_cap <= options.budget
        and 0 <= options.fee_rate < 0.05
    ):
        return "sizing_invalid"
    return None


def _portfolio_blocker(options, as_of):
    if options.portfolio_file is None:
        return "portfolio_missing"
    try:
        snapshot = read_portfolio(options.portfolio_file, as_of=as_of)
    except ValueError:
        return "portfolio_unavailable"
    if snapshot.available_cash_usdt <= 0 and not (
        options.all_pairs and any(balance > 0 for balance in snapshot.quote_balances.values())
    ):
        return "portfolio_cash_unavailable"
    if _finite_number(options.risk_cap) and 0 < options.risk_cap <= snapshot.stressed_risk_usdt:
        return "portfolio_risk_exhausted"
    return None


def _recorded_history_status(options, as_of):
    if options.history_file is None:
        return "missing", 0, 0
    try:
        rows = read_recorded_history(options.history_file)
    except (OSError, ValueError):
        return "unavailable", 0, 0
    if not rows:
        return "empty", 0, 0

    cohorts = defaultdict(list)
    symbols = None if options.all_pairs else frozenset(options.symbols)
    for row in rows:
        if (row.get("venue") != options.venue or row.get("strategy") != BASELINE
                or row.get("provenance") != RECORDED_PROVENANCE):
            continue
        family, symbol = row.get("family"), row.get("symbol")
        if not isinstance(family, str) or not family.strip() or not isinstance(symbol, str) or not symbol.strip():
            continue
        if symbols is None or symbol in symbols:
            cohorts[family, symbol].append(row)
    if not cohorts:
        return "not_comparable", 0, 0

    supported = 0
    for (family, symbol), cohort in cohorts.items():
        assessment = assess_recorded_history(
            cohort, family=family, venue=options.venue, symbol=symbol, strategy=BASELINE, as_of=as_of,
        )
        supported += assessment["status"] == "supported"
    return ("supported" if supported else "unqualified"), len(cohorts), supported


def readiness_report(options, *, as_of: float) -> dict:
    """Check operator-supplied inputs offline before any candidate-specific checks."""
    blockers = []
    if sizing := _sizing_blocker(options):
        blockers.append(sizing)
    if not options.team:
        blockers.append("team_disabled")

    manual_replies = options.manual_replies
    if manual_replies and (not options.telegram or getattr(options, "once", False)):
        blockers.append("manual_replies_unavailable")
    daily_cap = getattr(options, "daily_risk_cap", None)
    if daily_cap is not None and not (
        manual_replies and _finite_number(daily_cap) and _finite_number(options.budget)
        and 0 < daily_cap <= options.budget
    ):
        blockers.append("daily_risk_cap_invalid")

    if portfolio := _portfolio_blocker(options, as_of):
        blockers.append(portfolio)
    history, eligible_cohorts, supported_cohorts = _recorded_history_status(options, as_of)
    if history != "supported":
        blockers.append(f"history_{history}")

    return {
        "status": "research_only" if blockers else "candidate_review_required",
        "blockers": blockers,
        "manual_replies": "enabled" if manual_replies else "disabled",
        "recorded_history": {
            "status": history, "eligible_cohorts": eligible_cohorts, "supported_cohorts": supported_cohorts,
        },
    }
