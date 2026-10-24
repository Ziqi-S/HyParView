"""Node 单元测试：不启动 Simulator，只用一个最小假网络直接调用 Node 方法。"""
from collections import Counter
import unittest

from hyparview import Config, Message, Node


class FakeNetwork:
    """Implements the Network interface with no queue: sends are recorded only."""

    def __init__(self):
        self.nodes: dict[str, Node] = {}
        self.sent: list[tuple[str, str, Message]] = []
        self.records: Counter[str] = Counter()
        self.drops: Counter[str] = Counter()
        self.repairs: list[str] = []

    def add(self, node_id, active_size=2):
        node = Node(node_id, self, Config(active_size=active_size, passive_size=3))
        self.nodes[node_id] = node
        return node

    def lookup(self, node_id):
        return self.nodes.get(node_id)

    def send(self, source, target, message):
        self.sent.append((source, target, message))

    def probe(self, source, target):
        return target in self.nodes

    def schedule_repair(self, source, *, urgent=True):
        self.repairs.append(source)

    def record(self, source, kind):
        self.records[kind] += 1

    def record_drop(self, kind):
        self.drops[kind] += 1


class NodeWithoutSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.net = FakeNetwork()
        self.a, self.b, self.c = (self.net.add(i) for i in "abc")

    def test_connect_updates_both_views(self):
        self.assertTrue(self.a.connect("b"))
        self.assertEqual((self.a.active, self.b.active), ({"b"}, {"a"}))
        self.assertEqual(self.net.records["NEIGHBOR_LOW"], 1)
        self.assertEqual(self.net.records["NEIGHBOR_ACK"], 1)

    def test_low_priority_is_rejected_when_full_high_priority_evicts(self):
        self.net.add("d")
        self.b.connect("c")
        self.b.connect("d")
        self.assertFalse(self.a.connect("b", high=False))
        self.assertEqual(self.net.records["NEIGHBOR_REJECT"], 1)
        self.assertTrue(self.a.connect("b", high=True))
        self.assertIn("a", self.b.active)
        self.assertEqual(len(self.b.active), 2)
        # The evicted neighbor was notified and queued its own repair.
        evicted = ({"c", "d"} - self.b.active).pop()
        self.assertNotIn("b", self.net.nodes[evicted].active)
        self.assertIn(evicted, self.net.repairs)

    def test_unreachable_peer_is_dropped_and_forgotten(self):
        self.a.passive.add("ghost")
        self.assertFalse(self.a.connect("ghost"))
        self.assertNotIn("ghost", self.a.passive)
        self.assertEqual(self.net.drops["NEIGHBOR"], 1)

    def test_join_and_shuffle_only_emit_messages(self):
        self.a.join("b")
        self.assertEqual(self.net.sent[-1][:2], ("a", "b"))
        self.assertEqual(self.net.sent[-1][2].kind, "JOIN")
        self.a.connect("b")
        self.a.passive.update({"x", "y"})
        request_id = self.a.shuffle()
        source, target, message = self.net.sent[-1]
        self.assertEqual((source, target, message.kind), ("a", "b", "SHUFFLE"))
        self.assertEqual(message.request_id, request_id)
        self.assertEqual(message.sample_ids[0], "a")


if __name__ == "__main__":
    unittest.main()
