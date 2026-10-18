"""Run: python demo.py --nodes 100 --seed 42 --mode fixed"""
import argparse
import json
from pathlib import Path

from hyparview.experiments import experiment


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--mode", choices=["fixed", "capacity"], default="fixed")
    parser.add_argument("--failure-fraction", type=float, default=0.2)
    parser.add_argument("--failure-mode", choices=["random", "high-capacity"], default="random")
    parser.add_argument("--output", default=None)
    args = vars(parser.parse_args(argv))
    output = args.pop("output")
    try:
        result = experiment(**args)
    except ValueError as error:
        parser.error(str(error))
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    summary = {"mode": result["mode"], "victims": len(result["victims"]),
               "first_connected_round": result["first_connected_round_after_failure"],
               "broadcast": result["broadcast"]}
    for label in ("before_failure", "after_failure"):
        summary[label] = {k: v for k, v in result[label].items()
                          if not isinstance(v, dict)}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
