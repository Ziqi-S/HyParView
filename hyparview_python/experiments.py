"""Repeated HyParView simulation experiments using only the standard library.

Run ``python3 experiments.py --output results/experiment.json``. Broadcasts
after the crash run consecutively, without membership ticks between messages.
The approximate confidence intervals summarize variation between seeds; they
are not a reproduction of the paper's published confidence intervals."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from hyparview import Config
from hyparview.experiments import aggregate_runs, estimate, run_experiment, run_suite


def _csv_parser(convert, label):
    def parse(value):
        try:
            pieces = value.split(",")
            if any(not piece.strip() for piece in pieces):
                raise ValueError
            result = [convert(piece.strip()) for piece in pieces]
            if len(set(result)) != len(result):
                raise ValueError
            return result
        except ValueError as error:
            raise argparse.ArgumentTypeError(
                f"{label} must be a comma-separated list of unique values") from error
    return parse


def _path_samples(value):
    if value == "exact":
        return None
    try:
        result = int(value)
        if result < 1:
            raise ValueError
        return result
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "path-samples must be a positive integer or 'exact'") from error


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=_csv_parser(int, "nodes"), default=[100])
    parser.add_argument("--seeds", type=_csv_parser(int, "seeds"), default=[0, 1, 2])
    parser.add_argument("--failure-fractions", type=_csv_parser(float, "failure-fractions"),
                        default=[0.2, 0.5, 0.8])
    parser.add_argument("--warmup-rounds", "--rounds", type=int, default=20)
    parser.add_argument("--broadcasts", type=int, default=30)
    parser.add_argument("--recovery-rounds", type=int, default=0)
    parser.add_argument("--contact-mode", choices=["single", "random"], default="single")
    parser.add_argument("--failure-detector", choices=["on-send", "connection"],
                        default="on-send")
    parser.add_argument("--path-samples", type=_path_samples, default=32)
    for name, value in asdict(Config()).items():
        parser.add_argument("--" + name.replace("_", "-"), type=int, default=value)
    parser.add_argument("--output", type=Path)
    args = vars(parser.parse_args(argv))
    output = args.pop("output")
    args["failure_detection"] = args.pop("failure_detector").replace("-", "_")
    config_values = {name: args.pop(name) for name in asdict(Config())}
    node_counts, seeds, failure_fractions = (
        args.pop("nodes"), args.pop("seeds"), args.pop("failure_fractions"))
    try:
        result = run_suite(node_counts, seeds, failure_fractions,
                           config=Config(**config_values), **args)
    except ValueError as error:
        parser.error(str(error))
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
    summary = {"runs": len(result["runs"]),
               "output": str(output.resolve()) if output else None,
               "results": [{
                   "nodes": group["nodes"],
                   "failure_fraction": group["failure_fraction_requested"],
                   "seeds": group["seeds"],
                   "average_broadcast_reliability": group["average_broadcast_reliability"],
                   "last_broadcast_reliability": group["broadcast_history"][-1]["fraction"],
                   "connected_after_broadcasts":
                       group["metrics"]["after_broadcasts"]["connected_fraction"],
               } for group in result["aggregates"]]}
    print(json.dumps(summary, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
