"""Evaluate attributed real executions supplied in a local export."""

import json
from pathlib import Path

from news_quant import assess_outcomes

RECORDED_PROVENANCE = "recorded_fill"
RECORDED_ONLY = frozenset({RECORDED_PROVENANCE})
LIVE_LOOKBACK_DAYS = 3 * 365
RECENT_LOOKBACK_DAYS = 180
MIN_RECENT_EVENTS = 3
COHORT_FIELDS = ("family", "venue", "symbol", "strategy")
EXPORT_FIELDS = (
    *COHORT_FIELDS,
    "provenance",
    "outcome_id",
    "event_id",
    "event_at",
    "opened_at",
    "closed_at",
    "available_at",
    "risk_usdt",
    "net_pnl_usdt",
    "stressed_net_pnl_usdt",
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_nonfinite(_value):
    raise ValueError("Non-finite JSON number")


def read_recorded_history(path: Path | str | None) -> list[dict]:
    """Read a UTF-8 JSON array of execution records from an operator-supplied file.

    An absent export supplies no evidence. Reading the export cannot authenticate
    the exchange fills or confirm that the operator exported every outcome.
    """
    if not path:
        return []
    try:
        contents = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except UnicodeError as exc:
        raise ValueError("Журнал исполнений должен быть в кодировке UTF-8.") from exc
    if not contents.strip():
        return []
    try:
        records = json.loads(contents, object_pairs_hook=_unique_object, parse_constant=_reject_nonfinite)
    except (ValueError, RecursionError) as exc:
        raise ValueError("Журнал исполнений содержит некорректный JSON.") from exc
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ValueError("Журнал исполнений должен содержать массив объектов.")
    return [
        {field: record[field] for field in EXPORT_FIELDS if field in record}
        for record in records
    ]


def assess_recorded_history(
    records: list[dict],
    *,
    family: str,
    venue: str,
    symbol: str,
    as_of: float,
    before: float | None = None,
    strategy: str = "trend_v1",
) -> dict:
    """Screen completed executions with matching family, venue, symbol and strategy.

    Each outcome is one completed trade with positive initial risk, net PnL after
    actual costs and net PnL after additional cost stress, all denominated in USDT.
    All four timestamps are UTC epoch seconds. available_at describes when the final
    outcome and its attribution were available to the strategy.
    """
    cohort = (family, venue, symbol, strategy)
    valid_cohort = all(isinstance(value, str) and value.strip() for value in cohort)
    cohort_records = []
    ignored_other_cohorts = 0
    ignored_unrecorded = 0
    if valid_cohort:
        for record in records:
            if not isinstance(record, dict):
                # Let the shared estimator report an invalid record.
                cohort_records.append(record)
            elif tuple(record.get(field) for field in COHORT_FIELDS) != cohort:
                ignored_other_cohorts += 1
            elif record.get("provenance") != RECORDED_PROVENANCE:
                ignored_unrecorded += 1
            else:
                cohort_records.append(record)

    result = assess_outcomes(
        cohort_records, as_of=as_of, before=before,
        allowed_provenance=RECORDED_ONLY, lookback_days=LIVE_LOOKBACK_DAYS,
    )
    recent = assess_outcomes(
        cohort_records, as_of=as_of, before=before,
        allowed_provenance=RECORDED_ONLY, lookback_days=RECENT_LOOKBACK_DAYS,
    )
    recent_mean = recent["mean_stressed_net_r"]
    if not valid_cohort:
        result["reason"] = "invalid_cohort"
    elif result["status"] == "supported":
        if recent["event_count"] < MIN_RECENT_EVENTS:
            result.update(status="watch", reason="insufficient_recent_history")
        elif recent_mean is None or recent_mean <= 0:
            result.update(status="watch", reason="recent_stressed_edge_not_supported")
    result.update(
        lookback_days=LIVE_LOOKBACK_DAYS,
        recent_lookback_days=RECENT_LOOKBACK_DAYS,
        recent_event_count=recent["event_count"],
        recent_day_count=recent["day_count"],
        recent_mean_stressed_net_r=recent_mean,
        provenance=RECORDED_PROVENANCE,
        strategy=strategy,
        ignored_other_cohorts=ignored_other_cohorts,
        ignored_unrecorded=ignored_unrecorded,
    )
    return result
