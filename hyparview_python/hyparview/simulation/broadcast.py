"""应用层广播测量：静态图洪泛与触发故障修复的动态洪泛。"""
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..simulator import Simulator


def snapshot_broadcast(simulator: Simulator, origin: str) -> dict:
    """Separate ideal flooding measurement; no membership state changes.

    Each first-time recipient forwards to all neighbors except its sender.
    Duplicate deliveries are counted. This is not a HyParView message type.
    """
    graph = simulator.graph()
    if origin not in graph:
        raise ValueError("Broadcast origin must be alive")
    seen = {origin}
    queue = deque((origin, peer, 1) for peer in sorted(graph[origin]))
    attempts, duplicates, max_hops = 0, 0, 0
    while queue:
        sender, target, hops = queue.popleft()
        attempts += 1
        if target in seen:
            duplicates += 1
            continue
        seen.add(target)
        max_hops = max(max_hops, hops)
        queue.extend((target, p, hops + 1) for p in sorted(graph[target] - {sender}))
    return {"reached": len(seen), "fraction": len(seen) / len(graph),
            "transmissions": attempts, "duplicates": duplicates,
            "max_first_delivery_hops": max_hops}


def reactive_broadcast(simulator: Simulator, origin: str, *, reactive: bool = True,
                       max_events: int = 1_000_000) -> dict:
    """Flood using local active views, detecting failures during sends.

    At a failed send, queued local repair runs before this sender tries its
    new active links with the same payload. This abstracts reliable TCP
    failure/retry; the paper does not specify this exact event ordering.
    There are no cyclic membership ticks inside a broadcast. Deduplication
    lasts for this invocation, and each sender tries each address at most
    once. Data transmissions are counted separately from control traffic.

    reactive=False selects the old, non-mutating snapshot measurement.
    """
    if not reactive:
        return simulator.broadcast_snapshot(origin)
    if origin not in simulator.alive:
        raise ValueError("Broadcast origin must be alive")
    if type(max_events) is not int or max_events < 1:
        raise ValueError("max_events must be a positive integer")
    simulator.drain(max_events)
    seen = {origin}
    distances = {origin: 0}
    attempted: dict[str, set[str]] = {}
    incoming: dict[str, str | None] = {origin: None}
    deliveries: deque[tuple[str, str]] = deque()

    def forward(node_id: str):
        used = attempted.setdefault(node_id, set())
        peers = simulator.nodes[node_id].active - used
        parent = incoming[node_id]
        for peer in sorted(peers - {parent}):
            used.add(peer)
            deliveries.append((node_id, peer))

    forward(origin)
    attempts = duplicates = failed = steps = 0
    while deliveries:
        if steps >= max_events:
            raise RuntimeError("Broadcast event budget exceeded")
        steps += 1
        sender, target = deliveries.popleft()
        # forward() already emitted this message on a reliable connection.
        # Later membership eviction does not cancel an in-flight delivery.
        attempts += 1
        simulator.data_sent_by_node[sender] += 1
        if target not in simulator.alive:
            failed += 1
            simulator.nodes[sender].link_down(target, alive=False)
            simulator.drain(max_events)
            forward(sender)
            continue
        if target in seen:
            duplicates += 1
            continue
        seen.add(target)
        distances[target] = distances[sender] + 1
        incoming[target] = sender
        forward(target)
    simulator.check()
    return {
        "reached": len(seen), "fraction": len(seen) / len(simulator.alive),
        "transmissions": attempts, "duplicates": duplicates,
        "failed_attempts": failed,
        "max_first_delivery_hops": max(distances.values()),
    }

