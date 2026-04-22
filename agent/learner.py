"""A/B angle performance scoring.

Score formula:  likes + 3*comments + 5*reshares + 0.1*impressions
Weight per angle: Bayesian-smoothed mean with prior mean 50.0, prior weight 5.0.
Weights normalize to sum = 1.0.
"""
from __future__ import annotations

from typing import Iterable

PRIOR_MEAN = 50.0
PRIOR_WEIGHT = 5.0

ANGLES = ("technical_peer", "decision_maker", "mixed_story")


def score_row(row: dict) -> float:
    likes = row.get("likes") or 0
    comments = row.get("comments") or 0
    reshares = row.get("reshares") or 0
    impressions = row.get("impressions") or 0
    return float(likes) + 3.0 * comments + 5.0 * reshares + 0.1 * impressions


def angle_weights(rows: Iterable[dict]) -> list[dict]:
    """Return per-angle stats with normalized weights.

    Each dict: {angle, samples, avg_score, smoothed_mean, weight}.
    Weights sum to 1.0 across angles (equal split if no data anywhere).
    """
    by_angle: dict[str, list[float]] = {a: [] for a in ANGLES}
    for r in rows:
        a = r.get("angle")
        if a in by_angle:
            by_angle[a].append(score_row(r))

    smoothed: dict[str, float] = {}
    out: list[dict] = []
    for angle in ANGLES:
        scores = by_angle[angle]
        n = len(scores)
        mean = sum(scores) / n if n else 0.0
        smooth = (PRIOR_MEAN * PRIOR_WEIGHT + mean * n) / (PRIOR_WEIGHT + n)
        smoothed[angle] = smooth
        out.append({
            "angle": angle,
            "samples": n,
            "avg_score": round(mean, 2),
            "smoothed_mean": round(smooth, 2),
        })

    total = sum(smoothed.values()) or 1.0
    for entry in out:
        entry["weight"] = round(smoothed[entry["angle"]] / total, 4)
    return out
