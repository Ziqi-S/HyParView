"""JOIN admission and FORWARD_JOIN hop limits using the real simulator."""
from collections import deque
import unittest

from hyparview import Config, Message, Simulator


class ObservedQueue(deque):
    """Record live FORWARD_JOIN deliveries without changing FIFO behavior."""

    def __init__(self, simulator):
        super().__init__()
        self.simulator = simulator

    def popleft(self):
        source, target, message = super().popleft()
        if (message.kind == "FORWARD_JOIN"
                and source in self.simulator.alive
                and target in self.simulator.alive):
            self.simulator.forward_receipts.append((target, message.ttl))
        return source, target, message


class ObservedSimulator(Simulator):
    """Observe enqueue-time membership while retaining real protocol actions."""

    def __init__(self, seed=0):
        super().__init__(seed)
        self.forward_receipts = []
        self.forward_enqueues = []
        self.queue = ObservedQueue(self)

    def send(self, source, target, message):
        if message.kind == "FORWARD_JOIN" and source in self.alive:
            self.forward_enqueues.append({
                "source": source, "target": target, "message": message,
                "source_active": frozenset(self.nodes[source].active),
                "origin_active": frozenset(self.nodes[message.origin].active),
            })
        return super().send(source, target, message)


class JoinProtocolTests(unittest.TestCase):
    def deliver_join_only(self, simulator, contact, joiner):
        """Deliver the initial JOIN; leave all resulting work in the queue."""
        source, target, message = simulator.queue.popleft()
        self.assertEqual((source, target, message.kind), (joiner.id, contact.id, "JOIN"))
        self.assertIn(source, simulator.alive)
        self.assertIn(target, simulator.alive)
        contact.receive(source, message)

    def test_full_contact_admits_joiner_before_forward_join_delivery(self):
        simulator = ObservedSimulator(seed=6)
        contact = simulator.add("c", Config(active_size=2))
        old = [simulator.add(peer) for peer in ("a", "b")]
        for peer in old:
            contact.connect(peer.id, high=True)
        joiner = simulator.add("j", contact=contact.id)

        self.deliver_join_only(simulator, contact, joiner)

        retained = contact.active - {joiner.id}
        evicted = {peer.id for peer in old} - retained
        self.assertEqual(len(retained), 1)
        self.assertEqual(len(evicted), 1)
        self.assertEqual(joiner.active, {contact.id})
        self.assertIn(joiner.id, contact.active)
        self.assertIn(contact.id, simulator.nodes[next(iter(retained))].active)
        self.assertNotIn(contact.id, simulator.nodes[next(iter(evicted))].active)
        self.assertEqual(simulator.sent["DISCONNECT"], 1)
        queued = [(source, target, message)
                  for source, target, message in simulator.queue
                  if message.kind == "FORWARD_JOIN"]
        self.assertEqual({target for _, target, _ in queued}, retained)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][0], contact.id)
        self.assertEqual(queued[0][2].origin, joiner.id)
        self.assertEqual(simulator.forward_receipts, [])
        simulator.check()

    def test_joiner_is_active_at_both_ends_when_forward_join_is_enqueued(self):
        simulator = ObservedSimulator(seed=7)
        contact = simulator.add("c", Config(active_size=4))
        for peer in ("a", "b"):
            simulator.add(peer)
            contact.connect(peer, high=True)
        joiner = simulator.add("j", contact=contact.id)

        self.deliver_join_only(simulator, contact, joiner)

        self.assertEqual(len(simulator.forward_enqueues), 2)
        self.assertEqual({event["target"] for event in simulator.forward_enqueues}, {"a", "b"})
        for event in simulator.forward_enqueues:
            self.assertEqual(event["source"], contact.id)
            self.assertIn(joiner.id, event["source_active"])
            self.assertIn(contact.id, event["origin_active"])
            self.assertNotEqual(event["target"], joiner.id)
        self.assertEqual(simulator.forward_receipts, [])
        simulator.check()

    def test_arwl_counts_down_without_resetting_to_downstream_configuration(self):
        for arwl, prwl in ((0, 0), (1, 1), (3, 2)):
            with self.subTest(arwl=arwl, prwl=prwl):
                simulator = ObservedSimulator(seed=8)
                contact = simulator.add("c", Config(active_size=10, arwl=arwl, prwl=prwl))
                peers = [simulator.add(f"p{index}", Config(
                    active_size=10, arwl=8 + index, prwl=prwl))
                    for index in range(5)]
                chain = [contact, *peers]
                for source, target in zip(chain, chain[1:]):
                    source.connect(target.id, high=True)
                joiner = simulator.add("j", Config(active_size=10), contact=contact.id)

                simulator.drain()

                self.assertEqual(simulator.forward_receipts,
                                 [(f"p{index}", arwl - index)
                                  for index in range(arwl + 1)])
                self.assertEqual(joiner.active, {contact.id, f"p{arwl}"})
                self.assertIn(joiner.id, peers[arwl].active)
                passive_holders = {peer.id for peer in peers if joiner.id in peer.passive}
                expected_passive = {f"p{arwl - prwl}"} if prwl else set()
                self.assertEqual(passive_holders, expected_passive)
                self.assertNotIn(joiner.id, peers[arwl + 1].active)
                self.assertEqual(simulator.round, 0)
                simulator.check()

    def test_degree_one_dead_end_terminates_before_arwl_expires(self):
        simulator = ObservedSimulator(seed=9)
        config = Config(active_size=10, arwl=5, prwl=3)
        contact, p0, p1 = [simulator.add(peer, config) for peer in ("c", "p0", "p1")]
        contact.connect(p0.id, high=True)
        p0.connect(p1.id, high=True)
        joiner = simulator.add("j", config, contact=contact.id)

        simulator.drain()

        self.assertEqual(simulator.forward_receipts, [("p0", 5), ("p1", 4)])
        self.assertEqual(joiner.active, {contact.id, p1.id})
        self.assertIn(joiner.id, p1.active)
        self.assertEqual(simulator.round, 0)
        simulator.check()

    def test_unreachable_join_sender_does_not_trigger_forward_join(self):
        simulator = ObservedSimulator(seed=10)
        contact = simulator.add("c")
        old_neighbor = simulator.add("a")
        contact.connect(old_neighbor.id, high=True)
        before = {node_id: (set(node.active), set(node.passive))
                  for node_id, node in simulator.nodes.items()}

        contact.receive("unreachable", Message("JOIN"))

        self.assertEqual(simulator.forward_enqueues, [])
        self.assertFalse(simulator.queue)
        self.assertEqual(simulator.dropped["NEIGHBOR"], 1)
        self.assertEqual({node_id: (node.active, node.passive)
                          for node_id, node in simulator.nodes.items()}, before)
        simulator.check()


if __name__ == "__main__":
    unittest.main()
