"""理想仿真运行器：可靠 FIFO 投递、节点生命周期、调度和统计。

Simulator 用于组织多节点实验，不实现第二套连接规则或真实网络行为。
"""
from __future__ import annotations

from collections import Counter, deque
import random

from .config import Config
from .messages import Message
from .node import Node
from .simulation.broadcast import reactive_broadcast, snapshot_broadcast
from .simulation.metrics import collect_metrics


class Simulator:
    """Ideal simulation environment: reliable delivery, lifecycle and statistics.

    Implements the Network interface (network.py) that every Node talks to.

    Connection rules live only in Node.connect/disconnect (node.py).

    No latency, packet loss, concurrency or false suspicions. Crash knowledge is
    exposed through contact/probe failure or existing-link transport events,
    never through a global membership notification or global repair.
    No automatic global bootstrap oracle. IDs cannot be reused after a crash.
    """

    def __init__(self, seed: int = 0, *, failure_detection: str = "on_send"):
        if failure_detection not in {"on_send", "connection"}:
            raise ValueError("failure_detection must be 'on_send' or 'connection'")
        self.failure_detection = failure_detection
        self.rng = random.Random(seed)
        self.nodes: dict[str, Node] = {}
        self.alive: set[str] = set()
        self.queue: deque[tuple[str, str, Message]] = deque()
        self._repair_pending: set[str] = set()
        self._repair_urgent: set[str] = set()
        self._repair_deferred: set[str] = set()
        self.sent: Counter[str] = Counter()
        self.sent_by_node: Counter[str] = Counter()
        self.dropped: Counter[str] = Counter()
        self.data_sent_by_node: Counter[str] = Counter()
        self.round = 0

    def add(self, node_id: str, config: Config | None = None,
            contact: str | None = None) -> Node:
        if not isinstance(node_id, str) or not node_id or node_id in self.nodes:
            raise ValueError("Node ID must be a new nonempty string")
        node = Node(node_id, self, config, self.rng.randrange(2**63))
        self.nodes[node_id] = node
        self.alive.add(node_id)
        if contact is not None:
            node.join(contact)
        return node

    # --- Network interface used by Node (see network.py) ---

    def lookup(self, node_id: str) -> Node | None:
        """Find a live node by ID in all alive nodes, incl. active and passive view or None if unreachable."""
        return self.nodes[node_id] if node_id in self.alive else None

    # TODO：记录这个有啥用？
    def record(self, source: str, kind: str):
        """只记录控制消息次数，不执行协议操作。"""
        self.sent[kind] += 1
        self.sent_by_node[source] += 1

    # TODO：记录这个有啥用？
    def record_drop(self, kind: str):
        self.dropped[kind] += 1

    def send(self, from_id: str, to_id: str, message: Message):
        """将消息加入可靠 FIFO 队列；不设置丢包率或网络延迟。"""
        if from_id not in self.alive:
            return
        self.record(from_id, message.kind)
        #TODO:要不要计算data_sent_by_node？如果要计算，应该在这里加上
        self.queue.append((from_id, to_id, message))#TODO：这里的消息是直接加入队列的，没经过网络传输

    def schedule_repair(self, source: str, *, urgent: bool = True):
        if source not in self.alive:
            return
        if urgent:
            self._repair_urgent.add(source)
        self._repair_deferred.discard(source)
        if source in self.alive and source not in self._repair_pending:
            self._repair_pending.add(source)
            # Local work is not a transmitted protocol message.
            self.queue.append((source, source, Message("_REPAIR")))


    def pingpong_probe(self, source: str, target: str) -> bool:
        """True if target is reachable; otherwise record a dropped PING and return False."""
        self.record(source, "PING")
        if target in self.alive:
            self.record(target, "PONG")
            return True
        self.record_drop("PING")
        return False

    def drain(self, max_events: int = 1_000_000):
        """Process one finite scheduling epoch.

        Failure recovery is immediately eligible. Live-disconnect recovery is
        allowed once per node per epoch; repeated triggers become eligible on
        the next external drain (join, broadcast or cycle). This avoids endless
        high-priority eviction at degree one without disabling recovery.
        """
        if type(max_events) is not int or max_events < 1:
            raise ValueError("max_events must be a positive integer")
        for source in sorted(self._repair_deferred):
            self.schedule_repair(source, urgent=False)
        self._repair_deferred.clear()
        repaired: set[str] = set()
        steps = 0
        while self.queue:
            if steps >= max_events:
                raise RuntimeError("Event budget exceeded; possible protocol loop")
            steps += 1
            source, target, message = self.queue.popleft()
            if message.kind == "_LINK_FAILED":
                # A transport event on an existing connection, not a global
                # membership notification and not a transmitted packet.
                if source in self.alive:
                    self.nodes[source].handle_peer_disconnection(target, alive=False)
                continue
            if message.kind == "_REPAIR":
                self._repair_pending.discard(source)
                urgent = source in self._repair_urgent
                self._repair_urgent.discard(source)
                if source in self.alive:
                    if urgent or source not in repaired:
                        repaired.add(source)
                        self.nodes[source].repair()
                    else:
                        self._repair_deferred.add(source)
                continue
            if source not in self.alive or target not in self.alive:
                self.record_drop(message.kind)
                if source in self.alive and target not in self.alive:
                    self.nodes[source].handle_peer_disconnection(target, alive=False)
                continue
            self.nodes[target].receive(source, message)

    def tick(self, rounds: int = 1, *, probe_active: bool = True,
             fill: bool = False):
        if type(rounds) is not int or rounds < 0:
            raise ValueError("rounds must be a nonnegative integer")
        for _ in range(rounds):
            self.round += 1
            order = sorted(self.alive)
            self.rng.shuffle(order)
            for node_id in order:
                self.nodes[node_id].maintenance(probe_active_view=probe_active,
                                                fill=fill)
            self.drain()
            self.check()

    def crash(self, node_id: str):
        if node_id not in self.alive:
            raise ValueError("Node is not alive")
        self.alive.remove(node_id)
        # Deliberately leave stale entries in everyone else's views.
        if self.failure_detection == "connection":
            # Existing physical connection endpoints may receive a local TCP
            # close/error event even if they never receive the next broadcast.
            for peer in sorted(self.nodes[node_id].active & self.alive):
                self.queue.append((peer, node_id, Message("_LINK_FAILED")))

    def leave(self, node_id: str):
        if node_id not in self.alive:
            raise ValueError("Node is not alive")
        for peer in sorted(self.nodes[node_id].active):
            self.nodes[node_id].disconnect(peer, leaving=True)
        self.alive.remove(node_id)

    def check(self):
        for node_id in sorted(self.alive):
            node = self.nodes[node_id]
            node.check_local()
            for peer in node.active:
                if peer in self.alive:
                    assert node_id in self.nodes[peer].active, (node_id, peer)

    def graph(self) -> dict[str, set[str]]:
        # Only live, mutual links count; stale crash entries are excluded.
        return {i: {j for j in self.nodes[i].active & self.alive
                    if i in self.nodes[j].active} for i in sorted(self.alive)}

    def metrics(self, *, path_samples: int | None = None,
                with_paths: bool = True) -> dict:
        """Observe the overlay without affecting node state or protocol RNGs.

        Paths are exact by default. With path_samples, BFS uses a fixed seeded
        subset of LCC sources: average distance is an estimate and diameter is
        a lower bound. with_paths=False skips BFS for inexpensive time series.
        Empty local views have accuracy 1 (no stale entries).
        """
        return collect_metrics(self, path_samples=path_samples, with_paths=with_paths)

    def broadcast_snapshot(self, origin: str) -> dict:
        """Separate ideal flooding measurement; no membership state changes.

        Each first-time recipient forwards to all neighbors except its sender.
        Duplicate deliveries are counted. This is not a HyParView message type.
        """
        return snapshot_broadcast(self, origin)

    def broadcast(self, origin: str, *, reactive: bool = True,
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
        return reactive_broadcast(self, origin, reactive=reactive, max_events=max_events)

