"""Point-in-time evidence screen for completed, comparable outcomes."""

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass

DAY_SECONDS = 86_400
LOOKBACK_DAYS = 90
MIN_EVENTS = 20
MIN_EVENT_DAYS = 10
STANDARD_ERROR_MULTIPLIER = 2.5
PROVENANCE = frozenset({"paper_journal", "paper_replay"})


@dataclass(frozen=True)
class Outcome:
    event_id: str
    event_at: float
    opened_at: float
    closed_at: float
    available_at: float
    provenance: str
    risk: float
    net: float
    stressed: float


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        finite = float(value)
    except OverflowError:
        return None
    return finite if math.isfinite(finite) else None


def _eligible_outcomes(samples, start, cutoff, allowed_provenance):
    by_id: dict[str, Outcome] = {}
    conflicted: set[str] = set()
    invalid = 0
    for sample in samples:
        if not isinstance(sample, dict):
            invalid += 1
            continue
        event_at = _finite(sample.get("event_at"))
        available_at = _finite(sample.get("available_at"))
        if event_at is None or available_at is None or min(event_at, available_at) < 0:
            invalid += 1
            continue
        # Exclude unavailable revisions before validating their contents.
        if event_at < start or event_at >= cutoff or available_at >= cutoff:
            continue
        opened_at = _finite(sample.get("opened_at"))
        closed_at = _finite(sample.get("closed_at"))
        risk = _finite(sample.get("risk_usdt"))
        net = _finite(sample.get("net_pnl_usdt"))
        stressed = _finite(sample.get("stressed_net_pnl_usdt"))
        outcome_id, event_id = sample.get("outcome_id"), sample.get("event_id")
        provenance = sample.get("provenance")
        if (
            not isinstance(outcome_id, str) or not outcome_id.strip()
            or not isinstance(event_id, str) or not event_id.strip()
            or not isinstance(provenance, str) or provenance not in allowed_provenance
            or opened_at is None or closed_at is None
            or not event_at <= opened_at < closed_at <= available_at
            or risk is None or risk <= 0 or net is None or stressed is None or stressed > net
        ):
            invalid += 1
            continue
        entry = Outcome(event_id, event_at, opened_at, closed_at, available_at, provenance, risk, net, stressed)
        if outcome_id not in by_id:
            by_id[outcome_id] = entry
        elif by_id[outcome_id] != entry:
            invalid += 1
            conflicted.add(outcome_id)
    return [entry for key, entry in by_id.items() if key not in conflicted], invalid


def assess_outcomes(samples, *, as_of, before=None, allowed_provenance=PROVENANCE, lookback_days=LOOKBACK_DAYS):
    """Screen completed outcomes with information available strictly before the cutoff.

    The caller supplies one prespecified, comparable event family and strategy policy.
    The default window is 90 days and accepts paper provenance.
    Trade receipts cluster by the original event ID. Risk weights trades within each event,
    then event means receive equal weight within a UTC day. The lower stress estimate
    subtracts 2.5 standard errors across day blocks. It is a conservative screening
    statistic, not a calibrated confidence interval for dependent market returns.
    """
    result = {
        "status": "watch",
        "reason": "invalid_cutoff",
        "sample_count": 0,
        "event_count": 0,
        "day_count": 0,
        "excluded_count": len(samples),
        "invalid_count": 0,
        "mean_net_r": None,
        "mean_stressed_net_r": None,
        "lower_stressed_net_r": None,
    }
    now = _finite(as_of)
    cutoff = now if before is None else _finite(before)
    if now is None or cutoff is None or min(now, cutoff) <= 0:
        return result
    window = _finite(lookback_days)
    if window is None or window <= 0:
        result["reason"] = "invalid_lookback"
        return result
    cutoff = min(now, cutoff)
    start = max(0.0, now - window * DAY_SECONDS)
    observations, invalid = _eligible_outcomes(samples, start, cutoff, allowed_provenance)
    events: dict[str, list[Outcome]] = defaultdict(list)
    for outcome in observations:
        events[outcome.event_id].append(outcome)
    result.update(sample_count=len(observations), event_count=len(events),
                  excluded_count=len(samples) - len(observations), invalid_count=invalid)
    if invalid:
        result["reason"] = "invalid_outcome_history"
        return result

    daily: dict[int, list[tuple[float, float]]] = defaultdict(list)
    try:
        for group in events.values():
            event_at = group[0].event_at
            if any(entry.event_at != event_at for entry in group):
                raise ValueError("Receipts disagree on original event time")
            risk = math.fsum(entry.risk for entry in group)
            net_r = math.fsum(entry.net for entry in group) / risk
            stressed_r = math.fsum(entry.stressed for entry in group) / risk
            if not math.isfinite(net_r) or not math.isfinite(stressed_r):
                raise ValueError("Outcome ratio is not finite")
            daily[int(event_at) // DAY_SECONDS].append((net_r, stressed_r))
        result["day_count"] = len(daily)
        if not daily:
            result["reason"] = "insufficient_history"
            return result

        net_days = [statistics.fmean(net for net, _ in group) for group in daily.values()]
        stressed_days = [statistics.fmean(stress for _, stress in group) for group in daily.values()]
        result.update(mean_net_r=statistics.fmean(net_days), mean_stressed_net_r=statistics.fmean(stressed_days))
        if len(events) < MIN_EVENTS or len(daily) < MIN_EVENT_DAYS:
            result["reason"] = "insufficient_history"
            return result

        lower = (
            statistics.fmean(stressed_days)
            - STANDARD_ERROR_MULTIPLIER * statistics.stdev(stressed_days) / math.sqrt(len(daily))
        )
        if not math.isfinite(lower):
            raise ValueError("Lower stress estimate is not finite")
    except (OverflowError, ValueError):
        result.update(reason="invalid_outcome_history", mean_net_r=None, mean_stressed_net_r=None)
        return result

    result["lower_stressed_net_r"] = lower
    if lower <= 0:
        result["reason"] = "stressed_edge_not_supported"
        return result
    result.update(status="supported", reason="positive_stressed_edge")
    return result
