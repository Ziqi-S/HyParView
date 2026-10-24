"""Budget-controlled capacity pilot, separate from the original HyParView trials.

Run with ``python3 -m hyparview.experiments.capacity --help``. The simulation
uses only the standard library. Plotting is a separate optional module.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import random
import statistics

from .._version import __version__
from ..capacity import CapacityPolicy, ResourceProfile, apply_capacity
from ..config import Config
from ..simulation.metrics import graph_properties
from ..simulator import Simulator


VARIANTS = ("fixed", "static", "permuted", "adaptive")
FAILURE_VARIANTS = VARIANTS[:3]
POLICY = CapacityPolicy()
# Both mixes give relative capacities 1, 2, 4 and limits 3, 5, 7 (budget 5N).
# "decoupled" breaks the perfect CPU/RAM correlation: across the three classes
# the bottleneck is RAM, then RAM, then neither, and corr(CPU, RAM) = 0.
RESOURCE_MIXES = {
    "proportional": ((2, 4), (4, 8), (8, 16)),
    "decoupled": ((2, 12), (8, 8), (8, 16)),
}
DETECTORS = ("on_send", "connection")


def _integer(name, value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _plan(nodes: int, seed: int, resource_mix: str = "proportional") -> dict:
    ids = [f"n{i:05d}" for i in range(nodes)]
    profiles = [ResourceProfile(cpu, ram)
                for cpu, ram in RESOURCE_MIXES[resource_mix] for _ in range(nodes // 3)]
    random.Random(seed + 11000).shuffle(profiles)
    resources = dict(zip(ids, profiles))
    contact_rng = random.Random(seed + 12000)
    contacts = {node_id: (contact_rng.choice(ids[:index]) if index else None)
                for index, node_id in enumerate(ids)}
    limits = {i: POLICY.active_limit(resources[i]) for i in ids}
    shuffled = list(limits.values())
    random.Random(seed + 13000).shuffle(shuffled)
    permuted = dict(zip(ids, shuffled))
    swap_rng = random.Random(seed + 14000)
    count = max(1, int(nodes * 0.1))
    upgraded = swap_rng.sample([i for i in ids if POLICY.score(resources[i]) == 1], count)
    downgraded = swap_rng.sample([i for i in ids if POLICY.score(resources[i]) == 4], count)
    resources_after = dict(resources)
    for low, high in zip(upgraded, downgraded):
        resources_after[low], resources_after[high] = resources[high], resources[low]
    return {"ids": ids, "resources": resources, "resources_after": resources_after,
            "contacts": contacts, "limits": limits, "permuted": permuted,
            "resource_swaps": [{"low": low, "high": high}
                               for low, high in zip(upgraded, downgraded)],
            "changed_nodes": {"upgraded": sorted(upgraded),
                              "downgraded": sorted(downgraded)}}


def _build(plan: dict, variant: str, seed: int, warmup_rounds: int,
           *, observe_warmup: bool = False, detector: str = "on_send"):
    sim = Simulator(seed, failure_detection=detector)
    limits = ({i: 5 for i in plan["ids"]} if variant == "fixed"
              else plan["permuted"] if variant == "permuted" else plan["limits"])
    for node_id in plan["ids"]:
        sim.add(node_id, replace(Config(), active_size=limits[node_id]),
                plan["contacts"][node_id])
        sim.drain()
    previous = _counters(sim)
    history = []
    for step in range(warmup_rounds):
        sim.tick(probe_active=True, fill=False)
        if observe_warmup and step >= max(0, warmup_rounds - 5):
            record, previous = _timeline(sim, plan["resources"], plan["changed_nodes"],
                                         step - warmup_rounds, previous)
            history.append(record)
        else:
            previous = _counters(sim)
    return sim, history, previous


def _counters(sim: Simulator):
    return sum(sim.sent.values()), sum(len(n.events) for n in sim.nodes.values())


def _timeline(sim, resources, cohorts, cycle, previous):
    snapshot = sim.metrics(with_paths=False)
    graph = sim.graph()
    counters = _counters(sim)
    record = {key: snapshot[key] for key in (
        "largest_component_fraction", "components", "mean_degree", "edges",
        "active_limit_budget", "stale_active_entries", "mean_active_accuracy")}
    record.update(cycle=cycle, control_delta=counters[0] - previous[0],
                  link_events_delta=counters[1] - previous[1],
                  capacity_target_gap_mean=statistics.mean(
                      abs(len(graph[i]) - POLICY.active_limit(resources[i]))
                      for i in sim.alive))
    for name, ids in cohorts.items():
        record[f"{name}_degree_mean"] = statistics.mean(len(graph[i]) for i in ids)
        record[f"{name}_limit_mean"] = statistics.mean(
            sim.nodes[i].config.active_size for i in ids)
    return record, counters


def _structure(sim, resources):
    return graph_properties(sim.graph(), {i: POLICY.score(resources[i]) for i in sim.alive})


def _measure_window(sim, resources, seed, broadcasts):
    """Same origins for each variant; relative work is sends / broadcasts / c.

    Capacity never limits transmission service in this model. This measures a
    work proxy, not utilization. Broadcasts can drain pending local repairs.
    """
    previous = dict(sim.data_sent_by_node)
    control_before = sum(sim.sent.values())
    origin_rng = random.Random(seed + 15000)
    origins = [origin_rng.choice(sorted(sim.alive)) for _ in range(broadcasts)]
    results = [sim.broadcast(origin) for origin in origins]
    snapshot = sim.metrics(path_samples=32)
    work = {i: ((sim.data_sent_by_node[i] - previous.get(i, 0))
                / broadcasts / POLICY.score(resources[i])) for i in sorted(sim.alive)}
    total, squares = sum(work.values()), sum(v * v for v in work.values())
    snapshot.update(
        capacity_scores={i: POLICY.score(resources[i]) for i in sorted(sim.alive)},
        relative_data_work=work,
        jain_relative_data_work=(total * total / (len(work) * squares) if squares else None),
        p95_relative_data_work=sorted(work.values())[math.ceil(0.95 * len(work)) - 1],
        active_limit_utilization=(2 * snapshot["edges"] / snapshot["active_limit_budget"]),
        isolated_nodes=sum(d == 0 for d in snapshot["degrees"].values()),
        window_broadcast_origins=origins,
        window_mean_broadcast_fraction=statistics.mean(r["fraction"] for r in results),
        # Hop count to the last first delivery: a latency proxy in hops, not seconds.
        window_mean_last_delivery_hops=statistics.mean(
            r["max_first_delivery_hops"] for r in results),
        graph_properties=_structure(sim, resources),
        window_control_attempts=sum(sim.sent.values()) - control_before,
        window_application_attempts=sum(r["transmissions"] for r in results),
        window_broadcast_count=broadcasts,
    )
    # Group work is ALREADY divided by relative capacity, not raw work.
    for key, values in (("capacity_group_mean_degree", snapshot["degrees"]),
                        ("capacity_group_mean_work", work)):
        snapshot[key] = {str(c): statistics.mean(values[i] for i in sorted(sim.alive)
                                               if POLICY.score(resources[i]) == c)
                         for c in (1, 2, 4)}
    return snapshot


def _dynamic_run(plan, variant, seed, warmup_rounds, adaptation_rounds, broadcasts,
                 detector="on_send"):
    sim, history, previous = _build(plan, variant, seed, warmup_rounds,
                                    observe_warmup=True, detector=detector)
    before = _measure_window(sim, plan["resources"], seed, broadcasts)
    # Include the measurement window in the last prefailure observation, rather
    # than attributing its control work to the later resource event.
    record, previous = _timeline(sim, plan["resources"], plan["changed_nodes"], -1, previous)
    if history:
        record["control_delta"] += history[-1]["control_delta"]
        record["link_events_delta"] += history[-1]["link_events_delta"]
        history[-1] = record
    else:
        history.append(record)
    initial_limits = {i: sim.nodes[i].config.active_size for i in plan["ids"]}
    if variant == "adaptive":
        changes = sorted(plan["ids"], key=lambda i: (
            POLICY.active_limit(plan["resources_after"][i]) - sim.nodes[i].config.active_size, i))
        # Apply all decreases before increases, then run a single event epoch.
        for node_id in changes:
            apply_capacity(sim.nodes[node_id], plan["resources_after"][node_id], POLICY)
    sim.drain()
    record, previous = _timeline(sim, plan["resources_after"], plan["changed_nodes"], 0, previous)
    history.append(record)
    for cycle in range(1, adaptation_rounds + 1):
        sim.tick(probe_active=True, fill=False)
        record, previous = _timeline(sim, plan["resources_after"], plan["changed_nodes"], cycle, previous)
        history.append(record)
    final = _measure_window(sim, plan["resources_after"], seed, broadcasts)
    sim.check()
    return {"seed": seed, "variant": variant, "contacts": plan["contacts"],
            "profiles_before": {i: asdict(p) for i, p in plan["resources"].items()},
            "profiles_after": {i: asdict(p) for i, p in plan["resources_after"].items()},
            "limits_initial": initial_limits,
            "limits_final": {i: sim.nodes[i].config.active_size for i in plan["ids"]},
            "changed_nodes": plan["changed_nodes"],
            "resource_swaps": plan["resource_swaps"],
            "before": before, "final": final, "history": history}


def _failure_run(plan, variant, seed, warmup_rounds, broadcasts,
                 recovery_rounds, failure_fraction, failure_mode, detector="on_send"):
    sim, _, _ = _build(plan, variant, seed, warmup_rounds, detector=detector)
    before = sim.metrics(path_samples=32)
    before["graph_properties"] = _structure(sim, plan["resources"])
    failure_rng = random.Random(seed + 16000)
    order = failure_rng.sample(plan["ids"], len(plan["ids"]))
    if failure_mode == "high_capacity":
        # Stable sort retains the seeded random tie order within each class.
        order.sort(key=lambda i: -POLICY.score(plan["resources"][i]))
    victims = order[:int(len(order) * failure_fraction)]
    for victim in victims:
        sim.crash(victim)
    immediate = sim.metrics(with_paths=False)
    origins_rng = random.Random(seed + 17000)
    broadcast_history = []
    for index in range(broadcasts):
        origin = origins_rng.choice(sorted(sim.alive))
        control_before = sum(sim.sent.values())
        result = sim.broadcast(origin)
        broadcast_history.append({"broadcast": index + 1, "origin": origin, **result,
                                  "control_attempts": sum(sim.sent.values()) - control_before})
    after = sim.metrics(path_samples=32)
    after["graph_properties"] = _structure(sim, plan["resources"])
    keys = ("largest_component_fraction", "components", "stale_active_entries",
            "mean_active_accuracy", "mean_degree", "edges", "active_limit_budget",
            "control_attempts_total")
    recovery = [{"cycle": 0, **{key: after[key] for key in keys}}]
    first_connected = 0 if after["components"] == 1 else None
    for cycle in range(1, recovery_rounds + 1):
        sim.tick(probe_active=True, fill=False)
        snapshot = sim.metrics(with_paths=False)
        recovery.append({"cycle": cycle, **{key: snapshot[key] for key in keys}})
        if first_connected is None and snapshot["components"] == 1:
            first_connected = cycle
    sim.check()
    return {"seed": seed, "variant": variant, "contacts": plan["contacts"],
            "failure_mode": failure_mode, "failure_fraction": failure_fraction,
            "failure_fraction_actual": len(victims) / len(plan["ids"]),
            "profiles": {i: asdict(p) for i, p in plan["resources"].items()},
            "active_limits": {i: sim.nodes[i].config.active_size for i in plan["ids"]},
            "initial_total_capacity": sum(POLICY.score(p) for p in plan["resources"].values()),
            "lost_capacity": sum(POLICY.score(plan["resources"][i]) for i in victims),
            "surviving_capacity": sum(POLICY.score(plan["resources"][i]) for i in sim.alive),
            "victims": victims, "before": before, "immediately_after_failure": immediate,
            "broadcast_history": broadcast_history,
            "mean_broadcast_fraction": statistics.mean(r["fraction"] for r in broadcast_history),
            "after_broadcasts": after, "recovery_history": recovery,
            "first_connected_recovery_cycle": first_connected,
            "after_recovery": sim.metrics(path_samples=32)}


def run_capacity_suite(nodes=120, seeds=(0, 1, 2, 3, 4), warmup_rounds=30,
                       adaptation_rounds=20, broadcasts=20, recovery_rounds=10,
                       failure_fractions=(0, 0.2, 0.5, 0.8),
                       resource_mix="proportional", failure_detection="on_send") -> dict:
    """A deliberately small first study, not the final thesis evaluation.

    Equal resource classes and paired swaps guarantee initial total caps 5N.
    The local policy does not guarantee this budget for arbitrary populations.
    Crashes can remove different degree budgets; no global reallocation follows.
    """
    _integer("nodes", nodes, 12)
    if nodes % 6:
        raise ValueError("nodes must be divisible by 6 for equal classes and an even budget")
    for name, value in (("warmup_rounds", warmup_rounds),
                        ("adaptation_rounds", adaptation_rounds),
                        ("recovery_rounds", recovery_rounds)):
        _integer(name, value)
    _integer("broadcasts", broadcasts, 1)
    if resource_mix not in RESOURCE_MIXES:
        raise ValueError(f"resource_mix must be one of {sorted(RESOURCE_MIXES)}")
    if failure_detection not in DETECTORS:
        raise ValueError(f"failure_detection must be one of {list(DETECTORS)}")
    seeds, fractions = list(seeds), list(failure_fractions)
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be nonempty and unique")
    for seed in seeds:
        _integer("seed", seed)
    if not fractions or len(set(fractions)) != len(fractions):
        raise ValueError("failure_fractions must be nonempty and unique")
    for fraction in fractions:
        if (isinstance(fraction, bool) or not isinstance(fraction, (int, float))
                or not math.isfinite(fraction) or not 0 <= fraction < 1):
            raise ValueError("failure fractions must be finite and in [0, 1)")
    dynamic_runs, failure_runs = [], []
    for seed in seeds:
        plan = _plan(nodes, seed, resource_mix)
        for variant in VARIANTS:
            dynamic_runs.append(_dynamic_run(plan, variant, seed, warmup_rounds,
                                             adaptation_rounds, broadcasts,
                                             failure_detection))
        for fraction in fractions:
            for failure_mode in ("random", "high_capacity"):
                for variant in FAILURE_VARIANTS:
                    failure_runs.append(_failure_run(plan, variant, seed, warmup_rounds,
                                                     broadcasts, recovery_rounds,
                                                     fraction, failure_mode,
                                                     failure_detection))
    return {
        "schema_version": 2, "implementation_version": __version__,
        "settings": {"nodes": nodes, "seeds": seeds, "warmup_rounds": warmup_rounds,
                     "adaptation_rounds": adaptation_rounds, "broadcasts": broadcasts,
                     "recovery_rounds": recovery_rounds, "failure_fractions": fractions,
                     "failure_detection": failure_detection, "resource_mix": resource_mix,
                     "probe_active": True, "fill": False,
                     "policy": asdict(POLICY), "base_config": asdict(Config()),
                     "resource_classes": [
                         {"cpu": cpu, "ram_gib": ram,
                          "relative_capacity": POLICY.score(ResourceProfile(cpu, ram)),
                          "active_limit": POLICY.active_limit(ResourceProfile(cpu, ram))}
                         for cpu, ram in RESOURCE_MIXES[resource_mix]],
                     "rng_stream_offsets": {"profile": 11000, "contact": 12000,
                                            "permutation": 13000, "resource_swap": 14000,
                                            "workload": 15000, "failure": 16000,
                                            "failure_broadcast": 17000}},
        "model_notes": [
            "Known CPU/RAM inputs; bottleneck score and logarithmic degree mapping are modeling choices.",
            "No resource measurement, CPU service, RAM allocation, link delay or random loss model.",
            "Equal classes and paired resource swaps preserve cap budget 5N, not necessarily actual edges.",
            "Failures remove initial budgets without reallocation; surviving budgets need not match.",
            "Work is application sends / broadcast count / relative capacity, not utilization or throughput.",
            "Adaptive applies one event-driven local candidate sweep on growth; no forced periodic fill.",
            "Resource swap and actuation are immediate at cycle 0, not a model of detection latency.",
            "Failure broadcasts have no interleaved cycles; recovery cycles begin after the broadcasts.",
            "Seed is the independent replication unit; broadcasts within one overlay are correlated.",
            "Paths use at most 32 LCC sources; sampled diameter is a lower bound.",
            "Last-delivery hops are a latency proxy in hops; there is no time model.",
        ],
        "dynamic_runs": dynamic_runs, "failure_runs": failure_runs,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=120)
    parser.add_argument("--seeds", default="0,1,2,3,4")
    parser.add_argument("--warmup-rounds", type=int, default=30)
    parser.add_argument("--adaptation-rounds", type=int, default=20)
    parser.add_argument("--broadcasts", type=int, default=20)
    parser.add_argument("--recovery-rounds", type=int, default=10)
    parser.add_argument("--failure-fractions", default="0,0.2,0.5,0.8")
    parser.add_argument("--resource-mix", choices=sorted(RESOURCE_MIXES), default="proportional",
                        help="decoupled breaks the CPU/RAM correlation at the same budget")
    parser.add_argument("--failure-detection", choices=DETECTORS, default="on_send")
    parser.add_argument("--output", type=Path, default=Path("results/capacity_pilot.json"))
    args = parser.parse_args(argv)
    try:
        seeds = [int(v) for v in args.seeds.split(",")]
        fractions = [float(v) for v in args.failure_fractions.split(",")]
        result = run_capacity_suite(nodes=args.nodes, seeds=seeds,
                                    warmup_rounds=args.warmup_rounds,
                                    adaptation_rounds=args.adaptation_rounds,
                                    broadcasts=args.broadcasts,
                                    recovery_rounds=args.recovery_rounds,
                                    failure_fractions=fractions,
                                    resource_mix=args.resource_mix,
                                    failure_detection=args.failure_detection)
    except ValueError as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
                           encoding="utf-8")
    print(json.dumps({"output": str(args.output), "dynamic_runs": len(result["dynamic_runs"]),
                      "failure_runs": len(result["failure_runs"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
