"""AI probability calibration and tracking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from polyedge.core.db import Database


@dataclass
class CalibrationStats:
    """Tracking how well our AI predictions calibrate."""

    total_predictions: int = 0
    brier_score: float = 0.0  # Lower is better (0 = perfect, 1 = worst)
    mean_confidence: float = 0.0
    accuracy_by_bucket: dict = None  # {bucket: (predicted_avg, actual_avg, count)}

    def __post_init__(self):
        if self.accuracy_by_bucket is None:
            self.accuracy_by_bucket = {}


def calculate_brier_score(predictions: list[tuple[float, bool]]) -> float:
    """Calculate Brier score for a set of (probability, outcome) pairs.

    Brier score = mean((forecast - outcome)^2)
    0 = perfect, 1 = worst possible
    """
    if not predictions:
        return 0.0
    return sum((p - (1.0 if o else 0.0)) ** 2 for p, o in predictions) / len(predictions)


def calculate_calibration(
    predictions: list[tuple[float, bool]],
    n_buckets: int = 10,
) -> dict[str, tuple[float, float, int]]:
    """Calculate calibration curve — how well probabilities match actual rates.

    Returns dict of bucket -> (mean_predicted, mean_actual, count).
    Perfect calibration: predicted 0.7 → actually happens 70% of the time.
    """
    bucket_size = 1.0 / n_buckets
    buckets: dict[str, list[tuple[float, bool]]] = {}

    for prob, outcome in predictions:
        bucket_idx = min(int(prob / bucket_size), n_buckets - 1)
        bucket_key = f"{bucket_idx * bucket_size:.1f}-{(bucket_idx + 1) * bucket_size:.1f}"
        if bucket_key not in buckets:
            buckets[bucket_key] = []
        buckets[bucket_key].append((prob, outcome))

    result = {}
    for key, items in buckets.items():
        mean_pred = sum(p for p, _ in items) / len(items)
        mean_actual = sum(1.0 if o else 0.0 for _, o in items) / len(items)
        result[key] = (mean_pred, mean_actual, len(items))

    return result


def kelly_adjusted_by_calibration(
    raw_probability: float,
    confidence: float,
    calibration_stats: Optional[CalibrationStats] = None,
) -> float:
    """Adjust AI probability estimate based on historical calibration.

    If AI tends to be overconfident (Brier score high), pull estimates toward 50%.
    If AI is well-calibrated, trust the raw estimate more.
    """
    if calibration_stats is None or calibration_stats.total_predictions < 20:
        # Not enough data — apply conservative shrinkage toward 50%
        shrinkage = 0.3  # Pull 30% toward 50%
        return raw_probability * (1 - shrinkage) + 0.5 * shrinkage

    # Use Brier score to determine shrinkage
    # Good Brier: 0.1-0.2 → low shrinkage
    # Bad Brier: 0.3+ → high shrinkage
    brier = calibration_stats.brier_score
    if brier < 0.15:
        shrinkage = 0.1
    elif brier < 0.25:
        shrinkage = 0.2
    else:
        shrinkage = 0.4

    # Also factor in confidence
    shrinkage *= (1 - confidence * 0.5)  # High confidence → less shrinkage

    adjusted = raw_probability * (1 - shrinkage) + 0.5 * shrinkage
    return max(0.01, min(0.99, adjusted))


async def get_calibration_stats(db: Database, provider: str = "") -> CalibrationStats:
    """Calculate calibration stats from historical predictions.

    Requires resolved markets in the database.
    """
    # This will be populated as trades resolve
    # For now, return empty stats
    return CalibrationStats()
