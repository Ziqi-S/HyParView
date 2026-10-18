"""Aggregation and uncertainty estimates for repeated simulation runs."""
from __future__ import annotations

import json
import math
from statistics import mean, stdev


def estimate(values) -> dict:
    """Mean and exploratory normal 95% CI across independent run seeds.

    A single observation does not support a variance estimate. Bounds remain
    unbounded, including for proportions, to avoid concealing the approximation.
    """
    values = list(values)
    if not values:
        return {"n": 0, "mean": None, "stdev": None, "ci95": None}
    average = mean(values)
    if len(values) == 1:
        return {"n": 1, "mean": average, "stdev": None, "ci95": None}
    deviation = stdev(values)
    margin = 1.96 * deviation / math.sqrt(len(values))
    return {"n": len(values), "mean": average, "stdev": deviation,
            "ci95": [average - margin, average + margin]}


def aggregate_runs(runs: list[dict]) -> list[dict]:
    """Aggregate matching experimental designs and retain each reliability step."""
    design_fields = ("config", "warmup_rounds", "broadcasts", "recovery_rounds",
                     "contact_mode", "path_samples_requested", "failure_detection",
                     "implementation_version", "maintenance_policy")
    groups: dict[tuple[int, float, str], list[dict]] = {}
    for run in runs:
        design = {field: run[field] for field in design_fields}
        key = (run["nodes"], run["failure_fraction_requested"],
               json.dumps(design, sort_keys=True))
        groups.setdefault(key, []).append(run)
    aggregates = []
    for (nodes, failure_fraction, design), group in sorted(groups.items()):
        if len({run["seed"] for run in group}) != len(group):
            raise ValueError("Each aggregate requires distinct run seeds")
        summaries = {}
        for phase in ("before_failure", "immediately_after_failure",
                      "after_broadcasts", "after_recovery"):
            snapshots = [run[phase] for run in group]
            phase_summary = {
                "connected_fraction": estimate(
                    int(snapshot["components"] == 1) for snapshot in snapshots),
                "mean_degree": estimate(mean(snapshot["degrees"].values())
                                        for snapshot in snapshots),
            }
            for metric in ("components", "largest_component_fraction",
                           "mean_clustering", "average_path_length_lcc",
                           "diameter_lcc", "mean_active_accuracy",
                           "mean_passive_accuracy"):
                values = [snapshot[metric] for snapshot in snapshots
                          if isinstance(snapshot.get(metric), (int, float))]
                if values:
                    phase_summary[metric] = estimate(values)
            summaries[phase] = phase_summary
        curve = []
        for index in range(group[0]["broadcasts"]):
            records = [run["broadcast_history"][index] for run in group]
            curve.append({"broadcast": index + 1, **{
                metric: estimate(record[metric] for record in records)
                for metric in ("fraction", "transmissions", "duplicates",
                               "failed_attempts", "max_first_delivery_hops")}})
        recovered = [run["first_connected_recovery_round"] for run in group
                     if run["first_connected_recovery_round"] is not None]
        aggregates.append({
            "nodes": nodes, "failure_fraction_requested": failure_fraction,
            "design": json.loads(design),
            "run_count": len(group), "seeds": [run["seed"] for run in group],
            "metrics": summaries, "broadcast_history": curve,
            "average_broadcast_reliability": estimate(
                mean(record["fraction"] for record in run["broadcast_history"])
                for run in group),
            "broadcast_control_attempts": estimate(
                run["broadcast_control_attempts"] for run in group),
            "recovered_run_count": len(recovered),
            "unrecovered_run_count": len(group) - len(recovered),
            "first_connected_recovery_round_among_recovered": estimate(recovered),
        })
    return aggregates

