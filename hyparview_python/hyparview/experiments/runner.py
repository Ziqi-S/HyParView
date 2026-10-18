"""Reproducible single-run and multi-seed simulation experiments."""
from __future__ import annotations

from dataclasses import asdict
import math
import random

from ..config import Config
from ..simulator import Simulator
from .._version import __version__
from .statistics import aggregate_runs


def _nonnegative_integer(name: str, value: int, minimum: int = 0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def run_experiment(nodes: int = 100, seed: int = 0,
                   failure_fraction: float = 0.2, warmup_rounds: int = 20,
                   broadcasts: int = 30, recovery_rounds: int = 0,
                   contact_mode: str = "single", config: Config | None = None,
                   path_samples: int | None = 32,
                   failure_detection: str = "on_send") -> dict:
    """One sequential-join, warmup, crash and broadcast experiment.

    Contact, failure and broadcast choices have separate RNG streams, so using
    the same seed with another protocol configuration preserves these choices.
    Recovery ticks, when requested, occur only after the broadcast sequence.
    This recovery measures graph connectivity, whereas paper section 5.3 defines
    healing using the return of broadcast reliability to its prefailure level.
    """
    _nonnegative_integer("nodes", nodes, 2)
    _nonnegative_integer("seed", seed)
    _nonnegative_integer("warmup_rounds", warmup_rounds)
    _nonnegative_integer("broadcasts", broadcasts, 1)
    _nonnegative_integer("recovery_rounds", recovery_rounds)
    if (isinstance(failure_fraction, bool)
            or not isinstance(failure_fraction, (int, float))
            or not math.isfinite(failure_fraction)
            or not 0 <= failure_fraction < 1):
        raise ValueError("failure_fraction must be finite and in [0, 1)")
    if contact_mode not in {"single", "random"}:
        raise ValueError("contact_mode must be 'single' or 'random'")
    if failure_detection not in {"on_send", "connection"}:
        raise ValueError("failure_detection must be 'on_send' or 'connection'")
    if path_samples is not None:
        _nonnegative_integer("path_samples", path_samples, 1)
    config = config or Config()
    sim = Simulator(seed, failure_detection=failure_detection)
    stream_seeds = {"contact": seed + 10000, "failure": seed + 20000,
                    "broadcast": seed + 30000}
    contact_rng = random.Random(stream_seeds["contact"])
    failure_rng = random.Random(stream_seeds["failure"])
    broadcast_rng = random.Random(stream_seeds["broadcast"])
    contacts = {}
    for index in range(nodes):
        node_id = f"n{index:05d}"
        contact = None
        if index:
            contact = ("n00000" if contact_mode == "single"
                       else contact_rng.choice(sorted(sim.alive)))
        contacts[node_id] = contact
        sim.add(node_id, config, contact)
        sim.drain()  # Complete the join, but do not run a membership tick.
    after_join = sim.metrics(with_paths=False)
    sim.tick(warmup_rounds)
    before_failure = sim.metrics(path_samples=path_samples)
    victims = failure_rng.sample(sorted(sim.alive), int(nodes * failure_fraction))
    for victim in victims:
        sim.crash(victim)
    immediately_after_failure = sim.metrics(with_paths=False)
    broadcast_history = []
    for index in range(broadcasts):
        origin = broadcast_rng.choice(sorted(sim.alive))
        result = sim.broadcast(origin, reactive=True)
        broadcast_history.append({"broadcast": index + 1, "origin": origin,
                                  **result})
    after_broadcasts = sim.metrics(path_samples=path_samples)
    broadcast_control_attempts = (after_broadcasts["control_attempts_total"]
                                  - before_failure["control_attempts_total"])
    first_connected_recovery_round = (
        0 if after_broadcasts["components"] == 1 else None)
    recovery_history = [{"recovery_round": 0, **sim.metrics(with_paths=False)}]
    for recovery_round in range(1, recovery_rounds + 1):
        sim.tick()
        snapshot = sim.metrics(with_paths=False)
        recovery_history.append({"recovery_round": recovery_round, **snapshot})
        if first_connected_recovery_round is None and snapshot["components"] == 1:
            first_connected_recovery_round = recovery_round
    after_recovery = (sim.metrics(path_samples=path_samples)
                      if recovery_rounds else after_broadcasts)
    sim.check()
    return {
        "seed": seed, "nodes": nodes, "config": asdict(config),
        "implementation_version": __version__,
        "maintenance_policy": {"probe_active": True, "fill": False},
        "active_limit_budget": nodes * config.active_size,
        "failure_fraction_requested": failure_fraction,
        "failure_fraction_actual": len(victims) / nodes,
        "failure_count": len(victims), "victims": victims,
        "warmup_rounds": warmup_rounds, "broadcasts": broadcasts,
        "recovery_rounds": recovery_rounds, "contact_mode": contact_mode,
        "failure_detection": failure_detection,
        "contacts": contacts, "rng_stream_seeds": stream_seeds,
        "path_samples_requested": path_samples,
        "after_join": after_join, "before_failure": before_failure,
        "immediately_after_failure": immediately_after_failure,
        "broadcast_history": broadcast_history,
        "broadcast_control_attempts": broadcast_control_attempts,
        "after_broadcasts": after_broadcasts,
        "recovery_history": recovery_history, "after_recovery": after_recovery,
        "first_connected_recovery_round": first_connected_recovery_round,
    }


def run_suite(node_counts=(100,), seeds=(0, 1, 2),
              failure_fractions=(0.2, 0.5, 0.8), **kwargs) -> dict:
    node_counts, seeds, failure_fractions = (
        list(node_counts), list(seeds), list(failure_fractions))
    for name, values in (("node_counts", node_counts), ("seeds", seeds),
                         ("failure_fractions", failure_fractions)):
        if not values or len(set(values)) != len(values):
            raise ValueError(f"{name} must be nonempty and contain unique values")
    runs = [run_experiment(nodes=nodes, seed=seed,
                           failure_fraction=fraction, **kwargs)
            for nodes in node_counts for fraction in failure_fractions for seed in seeds]
    return {
        "schema_version": 2,
        "implementation_version": __version__,
        "model": "FIFO messages, reliable atomic links, selected ideal failure detector; "
                 "reactive broadcast repair and no membership ticks between broadcasts",
        "repair_policy": "Incoming live disconnects trigger recovery; local "
                         "evictions do not. Each node has one normal recovery "
                         "sweep per drain; repeated normal work is deferred to "
                         "the next drain. Failure sweeps are immediately eligible.",
        "interval_method": "Approximate normal 95% CI: mean +/- 1.96 * sample "
                           "stdev / sqrt(n). Null for n < 2; bounds are unbounded. "
                           "Small seed counts provide weak uncertainty estimates.",
        "path_note": "Path lengths and diameter may be estimated from sampled "
                     "sources. Consult paths_exact/path_sources in each snapshot; "
                     "a sampled diameter is a lower bound.",
        "recovery_note": "Connectivity recovery is measured after the broadcast "
                         "sequence. The mean excludes unrecovered runs, whose count "
                         "is reported separately; recovery rounds are not seconds. "
                         "This is not the paper section 5.3 healing metric, which "
                         "tests broadcast reliability against its prefailure level.",
        "runs": runs, "aggregates": aggregate_runs(runs),
    }

