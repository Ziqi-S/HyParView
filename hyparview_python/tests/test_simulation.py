import unittest

from demo import experiment
from hyparview import Config, Message, Simulator


class SimulationTests(unittest.TestCase):
    def test_full_initiator_low_priority_refusal_leaves_no_half_link(self):
        sim = Simulator()
        a, b, c = [sim.add(i, Config(active_size=1)) for i in "abc"]
        a.connect("b", high=True)
        before = {i: (set(node.active), set(node.passive))
                  for i, node in sim.nodes.items()}
        self.assertFalse(a.connect("c"))  # c has room; the initiator does not.
        self.assertEqual({i: (node.active, node.passive)
                          for i, node in sim.nodes.items()}, before)
        self.assertEqual(sim.sent["NEIGHBOR_REJECT"], 1)
        sim.check()

    def test_high_priority_connection_evicts_at_both_full_endpoints(self):
        sim = Simulator()
        a, b, c, d = [sim.add(i, Config(active_size=1)) for i in "abcd"]
        a.connect("b", high=True)
        c.connect("d", high=True)
        self.assertTrue(a.connect("c", high=True))
        self.assertEqual(a.active, {"c"})
        self.assertEqual(c.active, {"a"})
        self.assertFalse(b.active | d.active)
        self.assertIn("a", b.passive)
        self.assertIn("c", d.passive)
        self.assertEqual(sim.sent["DISCONNECT"], 2)
        sim.check()

    def test_connection_failure_notifications_wait_for_delivery(self):
        sim = Simulator(failure_detection="connection")
        a, b, c = [sim.add(i) for i in "abc"]
        a.connect("b", True)
        a.add_passive("c")
        sim.crash("b")
        self.assertIn("b", a.active)
        sim.drain()
        self.assertNotIn("b", a.active | a.passive)
        self.assertEqual(a.active, {"c"})
        self.assertEqual(sim.round, 0)
        self.assertNotIn("_LINK_FAILED", sim.sent)
        sim.check()

    def test_connection_detector_only_notifies_existing_connections(self):
        sim = Simulator(failure_detection="connection")
        a, b, c = [sim.add(i) for i in "abc"]
        a.connect("b", True)
        c.add_passive("b")
        sim.crash("b")
        sim.drain()
        self.assertIn("b", c.passive)  # no global death oracle
        self.assertFalse(c.active)
        self.assertFalse(a.active)
        with self.assertRaises(ValueError):
            Simulator(failure_detection="unknown")

    def test_reliable_queued_broadcast_survives_later_eviction(self):
        class EvictDuringRecovery(Simulator):
            inject = False

            def drain(self, max_events=1_000_000):
                super().drain(max_events)
                # Inject a membership eviction while a data delivery to z is
                # already queued. Its transport message must still arrive.
                if (self.inject and "dead" not in self.nodes["a"].active
                        and "z" in self.nodes["a"].active):
                    self.inject = False
                    self.nodes["a"].disconnect("z")
                    super().drain(max_events)

        sim = EvictDuringRecovery()
        a = sim.add("a")
        sim.add("dead")
        sim.add("r")
        sim.add("z", Config(passive_size=0))
        a.connect("dead", True)
        a.connect("z", True)
        a.add_passive("r")
        sim.crash("dead")
        sim.inject = True
        result = sim.broadcast("a")
        self.assertNotIn("z", a.active)
        self.assertEqual(result["reached"], 3)
        self.assertEqual(result["fraction"], 1)

    def test_capacity_increase_explicitly_schedules_refill(self):
        sim = Simulator()
        a = sim.add("a", Config(active_size=1))
        sim.add("b")
        sim.add("c")
        a.connect("b", True)
        a.add_passive("c")
        a.set_active_size(2)
        sim.drain()
        self.assertEqual(a.active, {"b", "c"})
        sim.check()

    def test_accuracy_exposes_stale_local_views(self):
        sim = Simulator()
        a, b, c = [sim.add(i) for i in "abc"]
        a.connect("b", True)
        a.add_passive("c")
        sim.crash("b")
        sim.crash("c")
        metrics = sim.metrics()
        self.assertEqual(metrics["degrees"], {"a": 0})
        self.assertEqual(metrics["active_occupancy"], {"a": 1})
        self.assertEqual(metrics["mean_active_accuracy"], 0)
        self.assertEqual(metrics["mean_passive_accuracy"], 0)
        self.assertEqual(metrics["stale_active_entries"], 1)
        self.assertEqual(metrics["stale_passive_entries"], 1)
        sim.broadcast("a")
        self.assertEqual(a.active | a.passive, set())
        self.assertEqual(sim.metrics()["mean_active_accuracy"], 1)

    def test_sampled_metrics_do_not_change_protocol_randomness(self):
        sims = [Simulator(9), Simulator(9)]
        for sim in sims:
            for i in range(30):
                sim.add(str(i), contact="0" if i else None)
                sim.drain()
        exact = sims[0].metrics()
        sampled = sims[0].metrics(path_samples=3)
        self.assertFalse(sampled["paths_exact"])
        self.assertEqual(sampled["path_sources"], 3)
        self.assertLessEqual(sampled["diameter_lcc"], exact["diameter_lcc"])
        self.assertIsNone(sims[0].metrics(with_paths=False)["diameter_lcc"])
        for sim in sims:
            sim.tick(3)
        self.assertEqual(sims[0].metrics(), sims[1].metrics())

    def test_stable_broadcast_matches_snapshot_and_separates_data_counts(self):
        sim = Simulator(5)
        for i in range(20):
            sim.add(str(i), contact="0" if i else None)
            sim.drain()
        control = sum(sim.sent.values())
        static = sim.broadcast_snapshot("0")
        actual = sim.broadcast("0")
        for key in static:
            self.assertEqual(actual[key], static[key])
        self.assertEqual(actual["failed_attempts"], 0)
        self.assertEqual(sum(sim.sent.values()), control)
        self.assertEqual(sum(sim.data_sent_by_node.values()), actual["transmissions"])

    def test_all_dead_candidates_repair_terminates_without_bootstrap_oracle(self):
        sim = Simulator()
        a, b = [sim.add(i) for i in "ab"]
        a.connect("b", True)
        a.passive.update({"dead1", "dead2"})
        sim.crash("b")
        result = sim.broadcast("a", max_events=20)
        self.assertEqual(result["fraction"], 1)
        self.assertFalse(a.active | a.passive)
        self.assertFalse(sim.queue)

    def test_shuffle_reply_sent_to_original_initiator(self):
        sim = Simulator()
        for i in "abcde":
            sim.add(i)
        sim.nodes["a"].connect("b", True)
        sim.nodes["b"].connect("c", True)
        sim.nodes["c"].connect("d", True)
        sim.nodes["d"].add_passive("e")
        sim.nodes["a"]._shuffles[10] = ()
        sim.send("a", "b", Message("SHUFFLE", "a", 3, ("a",), 10))
        sim.drain()
        self.assertIn("e", sim.nodes["a"].passive)
        self.assertEqual(sim.sent["SHUFFLE"], 3)
        self.assertEqual(sim.sent["SHUFFLE_REPLY"], 1)
        self.assertFalse(sim.nodes["a"]._shuffles)

    def test_invalid_simulation_and_demo_inputs(self):
        sim = Simulator()
        sim.add("a")
        for value in (-1, 1.5, True):
            with self.assertRaises(ValueError):
                sim.tick(value)
        for value in (0, -1, True):
            with self.assertRaises(ValueError):
                sim.drain(value)
            with self.assertRaises(ValueError):
                sim.metrics(path_samples=value)
            with self.assertRaises(ValueError):
                sim.broadcast("a", max_events=value)
        for options in ({"mode": "typo"}, {"failure_mode": "typo"},
                        {"nodes": True}, {"rounds": 2.5},
                        {"failure_fraction": float("nan")}):
            with self.assertRaises(ValueError):
                experiment(**options)
        with self.assertRaises(ValueError):
            sim.crash("missing")


if __name__ == "__main__":
    unittest.main()
