import unittest
from dataclasses import replace
from hyparview import Config, Message, Simulator
from demo import experiment


class ProtocolTests(unittest.TestCase):
    def test_config_validation(self):
        for kwargs in ({"active_size": 0}, {"passive_size": -1},
                       {"arwl": 1, "prwl": 2}, {"active_size": 2.5}):
            with self.assertRaises(ValueError):
                Config(**kwargs)

    def test_join_is_mutual(self):
        s = Simulator()
        a = s.add("a")
        b = s.add("b", contact="a")
        s.drain()
        self.assertEqual(a.active, {"b"})
        self.assertEqual(b.active, {"a"})
        s.check()

    def test_low_refusal_high_eviction(self):
        s = Simulator()
        cfg = Config(active_size=1)
        a, b, c = [s.add(i, cfg) for i in "abc"]
        self.assertTrue(a.connect("b", True))
        self.assertFalse(c.connect("b"))
        self.assertTrue(c.connect("b", True))
        self.assertEqual(a.active, set())
        self.assertEqual(b.active, {"c"})
        s.check()

    def test_promotion_removes_passive(self):
        s = Simulator()
        a, b = [s.add(i) for i in "ab"]
        a.add_passive("b")
        b.add_passive("a")
        a.repair()
        self.assertEqual(a.active, {"b"})
        self.assertFalse(a.passive | b.passive)
        s.check()

    def test_dead_end_forward_join(self):
        s = Simulator()
        a, b, c = [s.add(i) for i in "abc"]
        a.connect("b", True)
        b.receive("a", Message("FORWARD_JOIN", "c", 4))
        self.assertIn("c", b.active)
        s.check()

    def test_passive_walk_threshold(self):
        s = Simulator()
        a, b, c, d = [s.add(i) for i in "abcd"]
        a.connect("b", True)
        b.connect("c", True)
        b.receive("a", Message("FORWARD_JOIN", "d", b.config.prwl))
        self.assertIn("d", b.passive)
        self.assertEqual(s.queue[-1][2].ttl, b.config.prwl - 1)

    def test_shuffle_terminates_at_dead_end(self):
        s = Simulator()
        a, b, c = [s.add(i) for i in "abc"]
        a.connect("b", True)
        b.add_passive("c")
        a.shuffle()
        s.drain()
        self.assertIn("c", a.passive)
        self.assertFalse(a._shuffles)

    def test_overlapping_shuffle_replies_use_own_exported_peers(self):
        s = Simulator()
        a = s.add("a", Config(passive_size=2))
        a.passive.update({"b", "c"})
        a._shuffles = {1: ("b",), 2: ("c",)}
        a.receive("x", Message("SHUFFLE_REPLY", sample_ids=("d",), request_id=2))
        self.assertEqual(a.passive, {"b", "d"})
        a.receive("y", Message("SHUFFLE_REPLY", sample_ids=("e",), request_id=1))
        self.assertEqual(a.passive, {"d", "e"})
        a.receive("y", Message("SHUFFLE_REPLY", sample_ids=("z",), request_id=1))
        self.assertEqual(a.passive, {"d", "e"})

    def test_shuffle_reply_expires(self):
        s = Simulator()
        a = s.add("a")
        a._shuffles[1] = ()
        a.maintenance()
        a.receive("x", Message("SHUFFLE_REPLY", sample_ids=("y",), request_id=1))
        self.assertNotIn("y", a.passive)

    def test_crash_detected_on_tick_and_replaced(self):
        s = Simulator()
        a, b, c = [s.add(i) for i in "abc"]
        a.connect("b", True)
        a.add_passive("c")
        s.crash("b")
        self.assertIn("b", a.active)  # no omniscient removal
        s.tick()
        self.assertNotIn("b", a.active | a.passive)
        self.assertIn("c", a.active)

    def test_dead_candidate_removed_live_refusal_retained(self):
        s = Simulator()
        a = s.add("a", Config(active_size=2))
        b = s.add("b", Config(active_size=1))
        c, d = [s.add(i) for i in "cd"]
        b.connect("c", True)
        a.connect("d", True)
        a.passive.update({"b", "dead"})
        a.repair()
        self.assertIn("b", a.passive)
        self.assertNotIn("dead", a.passive)

    def test_graceful_leave(self):
        s = Simulator()
        a, b = [s.add(i) for i in "ab"]
        a.connect("b", True)
        s.leave("b")
        self.assertNotIn("b", a.active | a.passive)

    def test_zero_passive_and_bound_change(self):
        s = Simulator()
        a = s.add("a", Config(passive_size=0))
        for i in "bcde":
            s.add(i)
            a.connect(i, True)
        a.set_active_size(1)
        self.assertEqual(len(a.active), 1)
        self.assertFalse(a.passive)
        s.check()

    def test_metrics_disconnected_graph_and_flooding(self):
        s = Simulator()
        for i in "abcd":
            s.add(i)
        s.nodes["a"].connect("b", True)
        s.nodes["b"].connect("c", True)
        m = s.metrics()
        self.assertEqual(m["components"], 2)
        self.assertEqual(m["diameter_lcc"], 2)
        self.assertAlmostEqual(m["average_path_length_lcc"], 4 / 3)
        self.assertEqual(s.broadcast("a")["fraction"], 0.75)

    def test_isolated_node_does_not_use_global_registry(self):
        s = Simulator()
        a, b = [s.add(i) for i in "ab"]
        s.tick(3)
        self.assertFalse(a.active | b.active)
        a.join("b")
        s.drain()
        self.assertEqual(a.active, {"b"})

    def test_reproducible_and_bounded_churn(self):
        first = experiment(nodes=40, seed=7, rounds=5, mode="capacity")
        self.assertEqual(first, experiment(nodes=40, seed=7, rounds=5, mode="capacity"))
        for seed in range(5):
            s = Simulator(seed)
            for i in range(60):
                s.add(str(i), replace(Config(), active_size=3 + i % 6),
                      contact=str(i // 2) if i else None)
            s.drain()  # burst joins, not only sequential joins
            s.check()
            s.tick(5)
            for i in range(0, 60, 5):
                s.crash(str(i))
            s.tick(5)
            for i in range(1, 60, 11):
                if str(i) in s.alive:
                    s.leave(str(i))
            s.tick(3)
            for i in s.alive:
                self.assertTrue(s.nodes[i].active <= s.alive)


if __name__ == "__main__":
    unittest.main()
