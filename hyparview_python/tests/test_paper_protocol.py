"""Protocol conformance cases from Leitão et al. (2007), §§4.1–4.5.

TTL assertions distinguish FORWARD_JOIN's check-before-forward rule from
SHUFFLE's decrement-on-receipt rule. Fault cases run without a cyclic tick.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hyparview import Config, Message, Node, Simulator


class PaperProtocolTests(unittest.TestCase):
    def local_node(self, active=(), passive=(), config=None):
        """Observe a receive transition without draining its queued messages."""
        simulator = Simulator()
        node = simulator.add("receiver", config or Config())
        node.rng.seed(4)
        node.active.update(active)
        node.passive.update(passive)
        observations = SimpleNamespace(sent=[], connections=[])
        original_send = simulator.send
        original_connect = node.connect

        def observe_send(source, target, message):
            observations.sent.append((source, target, message))
            return original_send(source, target, message)

        def observe_connect(peer, high=False):
            if peer != node.id:
                observations.connections.append((node.id, peer, high))
            return original_connect(peer, high)

        send_patch = patch.object(simulator, "send", side_effect=observe_send)
        connect_patch = patch.object(node, "connect", side_effect=observe_connect)
        send_patch.start()
        connect_patch.start()
        self.addCleanup(send_patch.stop)
        self.addCleanup(connect_patch.stop)
        return node, observations

    def test_paper_evaluation_parameters(self):
        config = Config()
        self.assertEqual(
            (config.active_size, config.passive_size, config.arwl, config.prwl,
             config.k_a, config.k_p),
            (5, 30, 6, 3, 3, 4),
        )

    def test_forward_join_zero_ttl_connects_without_forwarding(self):
        node, observations = self.local_node(active=("sender", "other"))
        node.receive("sender", Message("FORWARD_JOIN", "new", ttl=0))
        self.assertEqual(observations.connections, [("receiver", "new", True)])
        self.assertEqual(observations.sent, [])

    def test_forward_join_ttl_one_forwards_zero_to_next_receiver(self):
        node, observations = self.local_node(active=("sender", "other"))
        node.receive("sender", Message("FORWARD_JOIN", "new", ttl=1))
        self.assertEqual(observations.connections, [])
        _, target, forwarded = observations.sent[0]
        self.assertEqual((target, forwarded.kind, forwarded.ttl),
                         ("other", "FORWARD_JOIN", 0))

    def test_forward_join_degree_one_terminates_after_sender_was_evicted(self):
        # A queued request may arrive after its sender ceased to be a neighbor.
        node, observations = self.local_node(active=("other",))
        node.receive("old_sender", Message("FORWARD_JOIN", "new", ttl=6))
        self.assertEqual(observations.connections, [("receiver", "new", True)])
        self.assertEqual(observations.sent, [])

    def test_forward_join_prwl_inserts_passive_before_forward(self):
        node, observations = self.local_node(active=("sender", "other"))
        node.receive("sender", Message("FORWARD_JOIN", "new", ttl=3))
        self.assertIn("new", node.passive)
        self.assertEqual(observations.connections, [])
        _, target, message = observations.sent[0]
        self.assertEqual((target, message.kind, message.ttl),
                         ("other", "FORWARD_JOIN", 2))

    def test_forward_join_excludes_previous_sender_only(self):
        node, observations = self.local_node(active=("sender", "new"))
        node.receive("sender", Message("FORWARD_JOIN", "new", ttl=5))
        self.assertEqual(observations.connections, [])
        self.assertEqual(observations.sent[0][1], "new")
        self.assertEqual(observations.sent[0][2].ttl, 4)

    def test_forward_join_at_own_origin_continues_bounded_walk(self):
        node, observations = self.local_node(active=("sender", "other"))
        node.receive("sender", Message("FORWARD_JOIN", "receiver", ttl=5))
        self.assertEqual(len(observations.sent), 1)
        _, target, forwarded = observations.sent[0]
        self.assertEqual((target, forwarded.origin, forwarded.ttl),
                         ("other", "receiver", 4))
        self.assertEqual(observations.connections, [])
        self.assertEqual(node.active, {"sender", "other"})
        self.assertNotIn("receiver", node.passive)

    def test_forward_join_at_own_origin_does_not_self_connect_at_terminal(self):
        node, observations = self.local_node(active=("sender", "other"))
        node.receive("sender", Message("FORWARD_JOIN", "receiver", ttl=0))
        self.assertEqual(observations.sent, [])
        self.assertEqual(observations.connections, [])

    def test_shuffle_ttl_one_terminates_at_current_receiver(self):
        node, observations = self.local_node(
            active=("sender", "other"), passive=("p", "q"))
        node.receive("sender", Message(
            "SHUFFLE", "origin", ttl=1, sample_ids=("origin", "new"),
            request_id=7))
        self.assertEqual(len(observations.sent), 1)
        _, target, reply = observations.sent[0]
        self.assertEqual((target, reply.kind, reply.request_id),
                         ("origin", "SHUFFLE_REPLY", 7))
        self.assertEqual(set(reply.sample_ids), {"p", "q"})
        self.assertTrue({"origin", "new"} <= node.passive)

    def test_shuffle_ttl_two_forwards_with_remaining_ttl_one(self):
        node, observations = self.local_node(active=("sender", "other"))
        node.receive("sender", Message(
            "SHUFFLE", "origin", ttl=2, sample_ids=("new",), request_id=8))
        _, target, forwarded = observations.sent[0]
        self.assertEqual((target, forwarded.kind, forwarded.ttl),
                         ("other", "SHUFFLE", 1))
        self.assertNotIn("new", node.passive)

    def test_shuffle_degree_one_terminates_after_sender_was_evicted(self):
        node, observations = self.local_node(active=("other",))
        node.receive("old_sender", Message(
            "SHUFFLE", "origin", ttl=6, sample_ids=("new",), request_id=9))
        self.assertEqual(observations.sent[0][1], "origin")
        self.assertEqual(observations.sent[0][2].kind, "SHUFFLE_REPLY")

    def test_shuffle_random_walk_can_return_to_origin(self):
        node, observations = self.local_node(active=("sender", "origin"))
        node.receive("sender", Message(
            "SHUFFLE", "origin", ttl=2, sample_ids=("new",), request_id=10))
        self.assertEqual(observations.sent[0][1], "origin")
        self.assertEqual(observations.sent[0][2].kind, "SHUFFLE")

    def test_stable_cyclic_rounds_do_not_refill_active_views(self):
        sim = Simulator(seed=5)
        a, b, c = [sim.add(i) for i in "abc"]
        a.connect("b", high=True)
        a.add_passive("c")
        b.add_passive("c")
        before = {i: set(node.active) for i, node in sim.nodes.items()}
        sim.tick(4)
        self.assertEqual({i: set(node.active) for i, node in sim.nodes.items()},
                         before)
        self.assertEqual(c.active, set())
        sim.check()

    def test_promotion_removes_candidate_from_both_passive_views(self):
        sim = Simulator(seed=6)
        a, b = [sim.add(i) for i in "ab"]
        a.add_passive("b")
        b.add_passive("a")
        a.repair()
        sim.drain()
        self.assertEqual(a.active, {"b"})
        self.assertEqual(b.active, {"a"})
        self.assertFalse(a.passive | b.passive)
        sim.check()

    def test_failed_candidate_removed_live_rejection_retained(self):
        sim = Simulator(seed=7)
        a = sim.add("a", Config(active_size=2))
        b = sim.add("b", Config(active_size=1))
        sim.add("c")
        sim.add("d")
        b.connect("c", high=True)
        a.connect("d", high=True)
        a.passive.update({"b", "unreachable"})
        a.repair()
        sim.drain()
        self.assertIn("b", a.passive)
        self.assertNotIn("b", a.active)
        self.assertNotIn("unreachable", a.passive)
        sim.check()

    def test_crash_is_discovered_and_repaired_by_broadcast_without_tick(self):
        sim = Simulator(seed=8)
        a, b, c = [sim.add(i) for i in "abc"]
        a.connect("b", high=True)
        a.add_passive("c")
        sim.crash("b")
        self.assertIn("b", a.active)
        result = sim.broadcast("a")
        self.assertEqual(sim.round, 0)
        self.assertNotIn("b", a.active | a.passive)
        self.assertEqual(a.active, {"c"})
        self.assertEqual(c.active, {"a"})
        self.assertEqual(result["fraction"], 1.0)
        sim.check()

    def test_snapshot_measurement_preserves_stale_crash_state(self):
        sim = Simulator(seed=9)
        a, b, c = [sim.add(i) for i in "abc"]
        a.connect("b", high=True)
        a.add_passive("c")
        sim.crash("b")
        result = sim.broadcast_snapshot("a")
        self.assertEqual(result["fraction"], 0.5)
        self.assertEqual(a.active, {"b"})
        self.assertEqual(a.passive, {"c"})

    def test_live_eviction_receiver_repairs_without_membership_tick(self):
        sim = Simulator(seed=10)
        a = sim.add("a", Config(active_size=2))
        b = sim.add("b", Config(active_size=2))
        c = sim.add("c", Config(active_size=2))
        d = sim.add("d", Config(active_size=2))
        sim.add("z", Config(active_size=1))
        a.connect("z", high=True)
        a.connect("d", high=True)
        a.add_passive("b")
        # z has only a to evict; its slot is then filled by c. a retains d,
        # so a cannot force z to accept low priority and must use backup b.
        c.connect("z", high=True)
        self.assertEqual(a.active, {"d"})
        sim.drain(max_events=100)
        self.assertEqual(sim.round, 0)
        self.assertEqual(a.active, {"b", "d"})
        self.assertEqual(b.active, {"a"})
        self.assertIn("z", a.passive)
        sim.check()

    def test_capacity_one_live_eviction_repair_has_bounded_epochs(self):
        # Three nodes of degree one cannot form a connected overlay. Unbounded
        # immediate high-priority retries would swap b's two candidates forever.
        sim = Simulator(seed=10)
        config = Config(active_size=1)
        a, b, c = [sim.add(i, config) for i in "abc"]
        a.connect("b", high=True)
        c.connect("b", high=True)
        sim.drain(max_events=100)
        for _ in range(3):
            sim.tick()
            sim.check()
            graph = sim.graph()
            self.assertEqual(sum(len(peers) for peers in graph.values()), 2)
            self.assertTrue(all(len(peers) <= 1 for peers in graph.values()))

    def test_repeated_live_recovery_resumes_on_next_external_drain(self):
        sim = Simulator()
        node = sim.add("a")
        original_repair = node.repair
        calls = []

        def observe_repair():
            calls.append("a")
            original_repair()
            if len(calls) == 1:
                # A second incoming live disconnect during the first sweep.
                sim.schedule_repair("a", urgent=False)

        node.repair = observe_repair
        sim.schedule_repair("a", urgent=False)
        sim.drain(max_events=10)
        self.assertEqual(calls, ["a"])
        sim.drain(max_events=10)
        self.assertEqual(calls, ["a", "a"])
        sim.drain(max_events=10)
        self.assertEqual(calls, ["a", "a"])

    def test_failed_contact_upgrades_pending_live_recovery(self):
        sim = Simulator()
        node = sim.add("a")
        original_repair = node.repair
        calls = []

        def observe_repair():
            calls.append("a")
            original_repair()
            if len(calls) == 1:
                # The ordinary allowance was used. A failed connection must
                # still make this pending work immediately eligible.
                sim.schedule_repair("a", urgent=False)
                sim.schedule_repair("a", urgent=True)

        node.repair = observe_repair
        sim.schedule_repair("a", urgent=False)
        sim.drain(max_events=10)
        self.assertEqual(calls, ["a", "a"])
        sim.drain(max_events=10)
        self.assertEqual(calls, ["a", "a"])

    def test_crash_cancels_deferred_live_recovery(self):
        sim = Simulator()
        node = sim.add("a")
        original_repair = node.repair
        calls = []

        def observe_repair():
            calls.append("a")
            original_repair()
            sim.schedule_repair("a", urgent=False)

        node.repair = observe_repair
        sim.schedule_repair("a", urgent=False)
        sim.drain(max_events=10)
        self.assertEqual(calls, ["a"])
        sim.crash("a")
        sim.drain(max_events=10)
        self.assertEqual(calls, ["a"])
        self.assertNotIn("a", sim.alive)


if __name__ == "__main__":
    unittest.main()
