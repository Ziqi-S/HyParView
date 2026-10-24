"""Plot capacity experiment JSON with optional matplotlib support.

Run ``python3 -m hyparview.experiments.plot_capacity --input results.json
--output-dir figures``. Each figure is exported as PNG and editable SVG.
Error bars and shaded bands describe sample standard deviations across seeds;
they are not confidence intervals. ``capacity_group_mean_work`` is already
normalized: mean per-node application sends / broadcasts / relative capacity.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
from statistics import mean, stdev


COLORS = {
    "fixed": "#505A64", "static": "#0072B2",
    "permuted": "#D55E00", "adaptive": "#009E73",
}
LABELS = {
    "fixed": "Fixed (all limits = 5)",
    "static": "Static (capacity-matched limits)",
    "permuted": "Permuted (shuffled limits)",
    "adaptive": "Adaptive (updates after change)",
}
MARKERS = {"fixed": "o", "static": "s", "permuted": "^", "adaptive": "D"}
STATIC_VARIANTS = ("fixed", "static", "permuted")
DYNAMIC_VARIANTS = ("fixed", "static", "adaptive")


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _summary(values):
    if not values:
        raise ValueError("Cannot summarize an empty set of observations")
    return mean(values), stdev(values) if len(values) > 1 else None


def _records_by_variant(records, variants, label):
    if not isinstance(records, list):
        raise ValueError(f"{label} must be a list")
    selected = {variant: [] for variant in variants}
    seen = set()
    for record in records:
        if not isinstance(record, dict) or "variant" not in record or "seed" not in record:
            raise ValueError(f"{label} entries require variant and seed")
        variant = record["variant"]
        if variant not in selected:
            continue
        key = variant, record["seed"]
        if key in seen:
            raise ValueError(f"Duplicate {label} record for variant {variant}, seed {record['seed']}")
        seen.add(key)
        selected[variant].append(record)
    for variant, group in selected.items():
        if not group:
            raise ValueError(f"{label} contains no records for variant {variant}")
    return selected


def _seed_note(groups):
    counts = [len(records) for records in groups.values()]
    if min(counts) == max(counts):
        return f"n = {counts[0]} seeds"
    return f"n = {min(counts)}–{max(counts)} seeds per curve"


def _style_axes(axis):
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)
    axis.grid(axis="y", color="#D7DDE3", linewidth=0.65)
    axis.set_axisbelow(True)
    axis.tick_params(direction="out", length=3.5)
    axis.margins(x=0.08)


def _plot_seed_summary(axis, xs, observations, variant, *, band=False):
    summaries = [_summary(values) for values in observations]
    averages = [summary[0] for summary in summaries]
    deviations = [summary[1] for summary in summaries]
    line, = axis.plot(xs, averages, color=COLORS[variant], linewidth=1.7,
                      marker=MARKERS[variant], markersize=4,
                      markerfacecolor="white", markeredgewidth=1.2,
                      label=LABELS[variant], zorder=3)
    valid = [index for index, deviation in enumerate(deviations) if deviation is not None]
    if band:
        # A single seed supplies no variance estimate, so no band is drawn.
        lows = [average - deviation if deviation is not None else math.nan
                for average, deviation in zip(averages, deviations)]
        highs = [average + deviation if deviation is not None else math.nan
                 for average, deviation in zip(averages, deviations)]
        if valid:
            axis.fill_between(xs, lows, highs, color=COLORS[variant], alpha=0.12,
                              linewidth=0, zorder=1)
    elif valid:
        axis.errorbar([xs[index] for index in valid],
                      [averages[index] for index in valid],
                      yerr=[deviations[index] for index in valid],
                      fmt="none", ecolor=COLORS[variant], elinewidth=1.1,
                      capsize=3, capthick=1.1, zorder=2)
    return line


def _legend(figure, handles, *, y=0.10):
    figure.legend(handles=handles, labels=[handle.get_label() for handle in handles],
                  loc="lower center", bbox_to_anchor=(0.5, y), ncol=len(handles),
                  frameon=False, fontsize=8.7, handlelength=2.4, columnspacing=1.6)


def _stable_figure(plt, data):
    groups = _records_by_variant(data["dynamic_runs"], STATIC_VARIANTS, "dynamic_runs")
    figure, axes = plt.subplots(1, 2, figsize=(11.6, 5.2))
    handles = []
    for axis, metric in zip(axes, ("capacity_group_mean_degree", "capacity_group_mean_work")):
        for variant in STATIC_VARIANTS:
            observations = []
            for capacity in ("1", "2", "4"):
                values = []
                for record in groups[variant]:
                    try:
                        value = record["before"][metric][capacity]
                    except (KeyError, TypeError) as error:
                        raise ValueError(f"dynamic_runs.before.{metric} requires capacity groups 1, 2, 4") from error
                    values.append(_number(value, f"before.{metric}.{capacity}"))
                observations.append(values)
            handle = _plot_seed_summary(axis, [0, 1, 2], observations, variant)
            if axis is axes[0]:
                handles.append(handle)
        axis.set_xticks([0, 1, 2])
        axis.set_xticklabels(["1", "2", "4"])
        axis.set_xlabel("Relative capacity class")
        axis.set_ylim(bottom=0)
        _style_axes(axis)
    axes[0].set_title("(a) Actual active degree")
    axes[0].set_ylabel("Mean actual degree")
    axes[1].set_title("(b) Capacity-normalized application work")
    axes[1].set_ylabel("Sends per broadcast / relative capacity")
    figure.suptitle(f"Capacity allocation before the resource change (N = {data['settings']['nodes']})",
                   fontsize=14, y=0.98)
    figure.subplots_adjust(left=0.075, right=0.98, top=0.85, bottom=0.28, wspace=0.28)
    _legend(figure, handles, y=0.11)
    budgets = sorted({_number(record["before"]["active_limit_budget"], "before.active_limit_budget")
                      for records in groups.values() for record in records})
    budget_note = (f"All active-limit budgets = {budgets[0]:g}; actual edge counts may differ."
                   if len(budgets) == 1 else
                   f"Active-limit budgets range from {budgets[0]:g} to {budgets[-1]:g}; "
                   "actual edge counts may differ.")
    figure.text(0.5, 0.02,
                f"Markers: across-seed means; error bars: sample SD ({_seed_note(groups)}). "
                "Adaptive is omitted because its initial policy matches Static.\n"
                f"{budget_note}\n"
                "Application work counts sends, including duplicates; it does not measure CPU use or throughput.",
                ha="center", va="bottom", fontsize=8.3, color="#404852")
    return figure


def _cycle_observations(groups, metric):
    output = {}
    for variant, records in groups.items():
        by_cycle = defaultdict(list)
        for record in records:
            if not isinstance(record.get("history"), list):
                raise ValueError("dynamic_runs.history must be a list")
            seen = set()
            for snapshot in record["history"]:
                try:
                    cycle = snapshot["cycle"]
                    value = snapshot[metric]
                except (KeyError, TypeError) as error:
                    raise ValueError(f"dynamic_runs.history requires cycle and {metric}") from error
                if type(cycle) is not int or cycle in seen:
                    raise ValueError("History cycles must be unique integers within each run")
                seen.add(cycle)
                by_cycle[cycle].append(_number(value, f"history.{metric}"))
        if not by_cycle:
            raise ValueError(f"No history observations for variant {variant}")
        output[variant] = by_cycle
    return output


def _adaptation_figure(plt, data):
    groups = _records_by_variant(data["dynamic_runs"], DYNAMIC_VARIANTS, "dynamic_runs")
    figure, axes = plt.subplots(1, 3, figsize=(13.3, 5.4))
    handles = []
    for axis, cohort, title in ((axes[0], "upgraded", "(a) Upgraded cohort"),
                                 (axes[1], "downgraded", "(b) Downgraded cohort")):
        actual = _cycle_observations(groups, f"{cohort}_degree_mean")
        limits = _cycle_observations(groups, f"{cohort}_limit_mean")
        for variant in DYNAMIC_VARIANTS:
            cycles = sorted(actual[variant])
            handle = _plot_seed_summary(axis, cycles,
                                        [actual[variant][cycle] for cycle in cycles],
                                        variant, band=True)
            if axis is axes[0]:
                handles.append(handle)
            limit_cycles = sorted(limits[variant])
            axis.plot(limit_cycles, [mean(limits[variant][cycle]) for cycle in limit_cycles],
                      color=COLORS[variant], linestyle="--", linewidth=1.3,
                      drawstyle="steps-post", alpha=0.85, zorder=2)
        axis.set_title(title)
        axis.set_ylabel("Mean actual degree / configured limit")
        axis.set_ylim(bottom=0)
    changes = _cycle_observations(groups, "link_events_delta")
    for variant in DYNAMIC_VARIANTS:
        cycles = sorted(changes[variant])
        _plot_seed_summary(axes[2], cycles, [changes[variant][cycle] for cycle in cycles],
                           variant, band=True)
    axes[2].set_title("(c) Topology perturbation")
    axes[2].set_ylabel("Local neighbor changes")
    axes[2].set_ylim(bottom=0)
    for axis in axes:
        axis.axvline(0, color="#8B9299", linewidth=1, linestyle=":", zorder=0)
        axis.set_xlabel("Membership cycle (resource change at 0)")
        _style_axes(axis)
    figure.suptitle(f"Response to a capacity change (N = {data['settings']['nodes']})",
                   fontsize=14, y=0.98)
    figure.subplots_adjust(left=0.06, right=0.985, top=0.85, bottom=0.29, wspace=0.30)
    _legend(figure, handles, y=0.12)
    figure.text(0.5, 0.02,
                "Solid: actual degree/local changes; dashed: configured degree limits; "
                f"shading: across-seed sample SD ({_seed_note(groups)}).\n"
                "Neighbor changes count both endpoints' local up/down events. "
                "Cycle 0 is an immediate event drain, not a full maintenance tick.\n"
                "Other cycles are maintenance rounds, not seconds. "
                "Permuted is omitted from the adaptation comparison.",
                ha="center", va="bottom", fontsize=8.3, color="#404852")
    return figure


def _failure_figure(plt, data):
    records = data["failure_runs"]
    if not isinstance(records, list):
        raise ValueError("failure_runs must be a list")
    figure, axes = plt.subplots(1, 2, figsize=(11.6, 5.4), sharey=True)
    handles = []
    seed_counts = []
    low, high = 0.0, 1.0
    for axis, mode, title in ((axes[0], "random", "(a) Random crashes"),
                               (axes[1], "high_capacity", "(b) Highest-capacity crashes")):
        groups = {variant: defaultdict(list) for variant in STATIC_VARIANTS}
        seen = set()
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("failure_runs entries must be objects")
            if record.get("failure_mode") != mode or record.get("variant") not in groups:
                continue
            try:
                fraction = _number(record["failure_fraction"], "failure_fraction")
                value = _number(record["mean_broadcast_fraction"], "mean_broadcast_fraction")
                key = record["variant"], fraction, record["seed"]
            except KeyError as error:
                raise ValueError("failure_runs requires failure_fraction, mean_broadcast_fraction and seed") from error
            if not 0 <= fraction < 1 or not 0 <= value <= 1:
                raise ValueError("Failure fractions must be in [0, 1); reached fractions must be in [0, 1]")
            if key in seen:
                raise ValueError(f"Duplicate failure observation: {mode}, {key}")
            seen.add(key)
            groups[record["variant"]][fraction].append(value)
        for variant in STATIC_VARIANTS:
            fractions = sorted(groups[variant])
            if not fractions:
                raise ValueError(f"No failure observations for {mode}, {variant}")
            observations = [groups[variant][fraction] for fraction in fractions]
            handle = _plot_seed_summary(axis, fractions, observations, variant)
            if axis is axes[0]:
                handles.append(handle)
            for values in observations:
                average, deviation = _summary(values)
                seed_counts.append(len(values))
                low = min(low, average - (deviation or 0))
                high = max(high, average + (deviation or 0))
        ticks = sorted({fraction for group in groups.values() for fraction in group})
        axis.set_xticks(ticks)
        axis.set_xticklabels([f"{fraction:.0%}" for fraction in ticks])
        axis.set_xlabel("Fraction of nodes crashed")
        axis.set_title(title)
        _style_axes(axis)
    axes[0].set_ylabel("Mean reached fraction of live nodes")
    axes[0].set_ylim(low - 0.025, high + 0.025)
    broadcasts = data["settings"].get("broadcasts", "the configured")
    detector = data["settings"].get("failure_detection", "unspecified")
    count = (f"n = {seed_counts[0]} seeds" if min(seed_counts) == max(seed_counts)
             else f"n = {min(seed_counts)}–{max(seed_counts)} seeds per point")
    figure.suptitle(f"Failure tolerance during post-crash broadcasts (N = {data['settings']['nodes']})",
                   fontsize=14, y=0.98)
    figure.subplots_adjust(left=0.075, right=0.98, top=0.85, bottom=0.30, wspace=0.17)
    _legend(figure, handles, y=0.12)
    figure.text(0.5, 0.02,
                f"Each run averages {broadcasts} consecutive broadcasts; error bars: across-seed sample SD ({count}).\n"
                f"No membership ticks between broadcasts; failure detector = {detector}. "
                "These are post-crash measurements, not final steady-state results.\n"
                "Active-limit budgets match before failure; surviving-node budgets may differ.",
                ha="center", va="bottom", fontsize=8.3, color="#404852")
    return figure


def _per_seed_points(axis, position, values, variant):
    average, deviation = _summary(values)
    axis.bar(position, average, width=0.62, color=COLORS[variant], alpha=0.85,
             label=LABELS[variant], zorder=2)
    if deviation is not None:
        axis.errorbar(position, average, yerr=deviation, fmt="none", ecolor="#17212B",
                      elinewidth=1, capsize=3, zorder=3)
    offsets = [(index - (len(values) - 1) / 2) * 0.07 for index in range(len(values))]
    axis.scatter([position + offset for offset in offsets], values, s=11,
                 color="white", edgecolor="#17212B", linewidth=0.7, zorder=4)


def _structure_figure(plt, data):
    groups = _records_by_variant(data["dynamic_runs"], STATIC_VARIANTS, "dynamic_runs")
    panels = (("capacity_degree_spearman", "(a) Capacity–degree rank correlation",
               "Spearman ρ(capacity, degree)"),
              ("degree_assortativity", "(b) Degree assortativity",
               "Pearson r over edge endpoints"),
              ("articulation_points", "(c) Single points of failure",
               "Articulation points"),
              ("window_mean_last_delivery_hops", "(d) Broadcast depth",
               "Hops to last first delivery"))
    figure, axes = plt.subplots(1, 4, figsize=(14.2, 4.9))
    for axis, (metric, title, label) in zip(axes, panels):
        for position, variant in enumerate(STATIC_VARIANTS):
            values = []
            for record in groups[variant]:
                before = record.get("before", {})
                value = before.get(metric, before.get("graph_properties", {}).get(metric))
                if value is None:
                    continue  # undefined correlation (constant input), not zero
                values.append(_number(value, f"before.{metric}"))
            if values:
                _per_seed_points(axis, position, values, variant)
        axis.axhline(0, color="#8B9299", linewidth=0.8)
        axis.set_xticks(range(len(STATIC_VARIANTS)))
        axis.set_xticklabels([variant.capitalize() for variant in STATIC_VARIANTS])
        axis.set_title(title)
        axis.set_ylabel(label)
        _style_axes(axis)
    handles = [plt.Rectangle((0, 0), 1, 1, color=COLORS[v], label=LABELS[v])
               for v in STATIC_VARIANTS]
    figure.suptitle(f"Overlay structure before the resource change (N = {data['settings']['nodes']})",
                   fontsize=14, y=0.98)
    figure.subplots_adjust(left=0.05, right=0.99, top=0.84, bottom=0.30, wspace=0.42)
    _legend(figure, handles, y=0.13)
    figure.text(0.5, 0.02,
                f"Bars: across-seed mean; error bars: sample SD; dots: individual seeds ({_seed_note(groups)}).\n"
                "Fixed limits give near-constant degree, so its correlation is close to 0 or undefined (omitted).\n"
                "Depth is measured in hops during the workload window; there is no time or latency model.",
                ha="center", va="bottom", fontsize=8.3, color="#404852")
    return figure


def _failure_timeline_figure(plt, data, fraction=0.5):
    records = data["failure_runs"]
    if not isinstance(records, list):
        raise ValueError("failure_runs must be a list")
    available = sorted({record.get("failure_fraction") for record in records
                        if isinstance(record, dict)} - {None})
    if not available:
        raise ValueError("failure_runs contains no failure fractions")
    fraction = min(available, key=lambda value: abs(value - fraction))
    figure, axes = plt.subplots(1, 3, figsize=(14.2, 5.0), sharey=True,
                                gridspec_kw={"width_ratios": (2, 2, 1.3)})
    handles = []
    for axis, mode, title in ((axes[0], "random", f"(a) Random {fraction:.0%} crash"),
                              (axes[1], "high_capacity", f"(b) Highest-capacity {fraction:.0%} crash")):
        for variant in STATIC_VARIANTS:
            runs = [r for r in records if r.get("failure_mode") == mode
                    and r.get("variant") == variant and r.get("failure_fraction") == fraction]
            if not runs:
                raise ValueError(f"No failure observations for {mode}, {variant}")
            length = min(len(r["broadcast_history"]) for r in runs)
            observations = [[_number(r["broadcast_history"][k]["fraction"], "fraction")
                             for r in runs] for k in range(length)]
            handle = _plot_seed_summary(axis, list(range(1, length + 1)), observations,
                                        variant, band=True)
            if axis is axes[0]:
                handles.append(handle)
        axis.set_xlabel("Broadcast index after crash (no maintenance ticks)")
        axis.set_title(title)
        _style_axes(axis)
    for variant in STATIC_VARIANTS:
        runs = [r for r in records if r.get("failure_mode") == "high_capacity"
                and r.get("variant") == variant and r.get("failure_fraction") == fraction]
        length = min(len(r["recovery_history"]) for r in runs)
        observations = [[_number(r["recovery_history"][k]["largest_component_fraction"],
                                 "largest_component_fraction") for r in runs]
                        for k in range(length)]
        _plot_seed_summary(axes[2], list(range(length)), observations, variant, band=True)
    axes[2].set_xlabel("Recovery tick (probes on)")
    axes[2].set_title("(c) Largest component, (b) continued")
    axes[0].set_ylabel("Reached / largest-component fraction of live nodes")
    axes[0].set_ylim(top=1.02)
    from matplotlib.ticker import MaxNLocator
    for axis in axes:
        axis.xaxis.set_major_locator(MaxNLocator(integer=True))
        _style_axes(axis)
    figure.suptitle(f"Time course after a {fraction:.0%} crash (N = {data['settings']['nodes']})",
                   fontsize=14, y=0.98)
    figure.subplots_adjust(left=0.06, right=0.99, top=0.85, bottom=0.29, wspace=0.12)
    _legend(figure, handles, y=0.12)
    detector = data["settings"].get("failure_detection", "unspecified")
    figure.text(0.5, 0.02,
                "Shading: across-seed sample SD. Each failed send triggers local repair (reactive recovery), "
                f"failure detector = {detector}.\n"
                "Recovery ticks start after the broadcasts and enable active probes; "
                "the two phases are measured separately.",
                ha="center", va="bottom", fontsize=8.3, color="#404852")
    return figure


def plot_capacity(input_file: str | Path, output_dir: str | Path) -> list[dict[str, str]]:
    """Read one capacity experiment JSON and export five PNG/SVG figure pairs."""
    with Path(input_file).open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or not isinstance(data.get("settings"), dict):
        raise ValueError("Input requires a settings object")
    for field in ("dynamic_runs", "failure_runs"):
        if field not in data:
            raise ValueError(f"Input requires {field}")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("Plotting requires optional matplotlib; install it with "
                           "'python3 -m pip install matplotlib'.") from error

    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    style = {
        "figure.facecolor": "white", "axes.facecolor": "white",
        "savefig.facecolor": "white", "font.family": "DejaVu Sans",
        "font.size": 10, "axes.titlesize": 11, "axes.labelsize": 10,
        "xtick.labelsize": 9, "ytick.labelsize": 9,
        "axes.edgecolor": "#727B84", "axes.linewidth": 0.7,
        "text.color": "#17212B", "axes.labelcolor": "#17212B",
        "xtick.color": "#38434E", "ytick.color": "#38434E",
        "svg.fonttype": "none", "svg.hashsalt": "hyparview-capacity",
    }
    outputs = []
    builders = (("01_capacity_steady_state", _stable_figure),
                ("02_capacity_adaptation", _adaptation_figure),
                ("03_capacity_failures", _failure_figure),
                ("04_capacity_structure", _structure_figure),
                ("05_capacity_failure_timeline", _failure_timeline_figure))
    with matplotlib.rc_context(style):
        for name, builder in builders:
            figure = builder(plt, data)
            png, svg = destination / f"{name}.png", destination / f"{name}.svg"
            try:
                figure.savefig(png, dpi=220)
                figure.savefig(svg, metadata={"Date": None})
            finally:
                plt.close(figure)
            outputs.append({"png": str(png), "svg": str(svg)})
    return outputs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Capacity experiment JSON")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        figures = plot_capacity(args.input, args.output_dir)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps({"figures": figures,
                      "error_bars": "Sample standard deviation across seeds; not a confidence interval"},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
