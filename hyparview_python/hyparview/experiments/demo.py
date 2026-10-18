"""Single-run topology recovery and capacity demonstration."""
from dataclasses import asdict, replace
import random

from ..config import Config
from ..simulator import Simulator
from .._version import __version__


def experiment(nodes=100, seed=42, mode="fixed", rounds=20,
               failure_fraction=0.2, failure_mode="random"):
    if (type(nodes) is not int or nodes < 2 or type(rounds) is not int
            or rounds < 0 or isinstance(failure_fraction, bool)
            or not isinstance(failure_fraction, (int, float))
            or not 0 <= failure_fraction < 1):
        raise ValueError("Require nodes >= 2, rounds >= 0, 0 <= failure_fraction < 1")
    if mode not in {"fixed", "capacity"}:
        raise ValueError("mode must be 'fixed' or 'capacity'")
    if failure_mode not in {"random", "high-capacity"}:
        raise ValueError("failure_mode must be 'random' or 'high-capacity'")
    sim = Simulator(seed)
    rng = random.Random(seed + 10000)
    # Same profile is recorded in BOTH modes to make normalization possible.
    capacities = {f"n{i:04}": (1, 2, 4)[i % 3] for i in range(nodes)}
    for node_id, capacity in capacities.items():
        limit = 5 if mode == "fixed" else min(12, max(3, 2 * capacity))
        contact = rng.choice(sorted(sim.alive)) if sim.alive else None
        sim.add(node_id, replace(Config(), active_size=limit), contact)
        sim.drain()
    sim.tick(rounds)
    before = sim.metrics(path_samples=128)
    count = int(nodes * failure_fraction)
    victims = (rng.sample(sorted(sim.alive), count) if failure_mode == "random"
               else sorted(sim.alive, key=lambda i: (-capacities[i], i))[:count])
    for victim in victims:
        sim.crash(victim)
    history = [sim.metrics(with_paths=False)]
    recovery_rounds = 0 if history[0]["components"] == 1 else None
    for r in range(1, rounds + 1):
        sim.tick()
        history.append(sim.metrics(with_paths=False))
        if recovery_rounds is None and history[-1]["components"] == 1:
            recovery_rounds = r
    after = sim.metrics(path_samples=128)
    # This demo reports topology recovery by keepalive rounds. Use the separate
    # experiments CLI for broadcast-triggered recovery before any next tick.
    broadcast = sim.broadcast_snapshot(min(sim.alive))
    return {
        "schema_version": 2,
        "implementation_version": __version__,
        "maintenance_policy": {"probe_active": True, "fill": False},
        "model": "FIFO control/repair queue; reliable atomic links; ideal keepalive "
                 "detection each tick; static final broadcast measurement",
        "config": asdict(Config()),
        "seed": seed, "mode": mode, "failure_mode": failure_mode,
        "capacity": capacities,
        "active_limits": {i: n.config.active_size for i, n in sim.nodes.items()},
        "before_failure": before, "victims": victims,
        "after_failure": after, "recovery_history": history,
        "first_connected_round_after_failure": recovery_rounds,
        "degree_per_capacity": {i: d / capacities[i] for i, d in after["degrees"].items()},
        "recovery_control_attempts_per_capacity": {
            i: (after["control_attempts_by_node"].get(i, 0)
                - before["control_attempts_by_node"].get(i, 0)) / capacities[i]
            for i in sorted(sim.alive)},
        "broadcast": broadcast,
    }

