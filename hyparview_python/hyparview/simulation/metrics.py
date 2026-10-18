"""只读仿真指标：连通性、路径、聚类、视图准确率和消息统计。"""
from __future__ import annotations

from collections import deque
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..simulator import Simulator


def collect_metrics(simulator: Simulator, *, path_samples: int | None = None,
            with_paths: bool = True) -> dict:
    """Observe the overlay without affecting node state or protocol RNGs.

    Paths are exact by default. With path_samples, BFS uses a fixed seeded
    subset of LCC sources: average distance is an estimate and diameter is
    a lower bound. with_paths=False skips BFS for inexpensive time series.
    Empty local views have accuracy 1 (no stale entries).
    """
    if path_samples is not None and (
            type(path_samples) is not int or path_samples < 1):
        raise ValueError("path_samples must be a positive integer or None")
    graph = simulator.graph()
    components = []
    unseen = set(graph)
    while unseen:
        root = min(unseen)
        component = {root}
        todo = [root]
        while todo:
            for peer in sorted(graph[todo.pop()] - component):
                component.add(peer)
                todo.append(peer)
        components.append(component)
        unseen -= component
    largest = max(components, key=len, default=set())
    distances = []
    sources = sorted(largest) if with_paths else []
    if path_samples is not None and len(sources) > path_samples:
        sources = sorted(random.Random(0).sample(sources, path_samples))
    paths_exact = with_paths and len(sources) == len(largest)
    for root in sources:
        d = {root: 0}
        todo = deque([root])
        while todo:
            current = todo.popleft()
            for peer in sorted(graph[current] - d.keys()):
                d[peer] = d[current] + 1
                todo.append(peer)
        distances.extend(v for k, v in d.items() if k != root)
    clustering = []
    for peers in graph.values():
        k = len(peers)
        directed_links = sum(len(graph[p] & peers) for p in peers)
        clustering.append(directed_links / (k * (k-1)) if k > 1 else 0.0)
    active_accuracy = {}
    passive_accuracy = {}
    stale_active = stale_passive = 0
    for node_id in sorted(simulator.alive):
        node = simulator.nodes[node_id]
        stale_a = len(node.active - simulator.alive)
        stale_p = len(node.passive - simulator.alive)
        stale_active += stale_a
        stale_passive += stale_p
        active_accuracy[node_id] = (1 - stale_a / len(node.active)
                                    if node.active else 1.0)
        passive_accuracy[node_id] = (1 - stale_p / len(node.passive)
                                     if node.passive else 1.0)
    degrees = {i: len(p) for i, p in graph.items()}
    return {
        "round": simulator.round, "alive": len(graph),
        "failure_detection": simulator.failure_detection,
        "components": len(components), "largest_component": len(largest),
        "largest_component_fraction": len(largest) / len(graph) if graph else 0.0,
        "diameter_lcc": max(distances, default=0) if with_paths else None,
        "average_path_length_lcc": (sum(distances) / len(distances)
                                    if distances else 0.0) if with_paths else None,
        "paths_exact": paths_exact, "path_sources": len(sources),
        "mean_clustering": sum(clustering) / len(graph) if graph else 0.0,
        "degrees": degrees,
        "mean_degree": sum(degrees.values()) / len(graph) if graph else 0.0,
        "min_degree": min(degrees.values(), default=0),
        "max_degree": max(degrees.values(), default=0),
        "edges": sum(degrees.values()) // 2,
        "active_limit_budget": sum(simulator.nodes[i].config.active_size for i in simulator.alive),
        "active_occupancy": {i: len(simulator.nodes[i].active) for i in sorted(simulator.alive)},
        "passive_occupancy": {i: len(simulator.nodes[i].passive) for i in sorted(simulator.alive)},
        "active_accuracy": active_accuracy, "passive_accuracy": passive_accuracy,
        "mean_active_accuracy": (sum(active_accuracy.values()) / len(graph)
                                 if graph else 1.0),
        "mean_passive_accuracy": (sum(passive_accuracy.values()) / len(graph)
                                  if graph else 1.0),
        "stale_active_entries": stale_active,
        "stale_passive_entries": stale_passive,
        "control_attempts_total": sum(simulator.sent.values()),
        "control_attempts_by_kind": dict(sorted(simulator.sent.items())),
        "control_attempts_by_node": dict(sorted(simulator.sent_by_node.items())),
        "dropped": dict(sorted(simulator.dropped.items())),
        "data_attempts_total": sum(simulator.data_sent_by_node.values()),
        "deferred_repair_nodes": len(simulator._repair_deferred & simulator.alive),
    }

