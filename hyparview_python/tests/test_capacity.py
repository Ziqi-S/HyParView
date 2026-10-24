"""Local capacity policy and paired, budget-controlled pilot regressions."""
from collections import Counter, defaultdict
import math
from types import SimpleNamespace
import unittest

from hyparview import Config, Simulator
from hyparview.capacity import CapacityPolicy, ResourceProfile, apply_capacity
from hyparview.experiments.capacity import run_capacity_suite
from hyparview.simulation.metrics import graph_properties


class CapacityPolicyTests(unittest.TestCase):
    def test_reference_profiles_map_to_three_five_seven(self):
        policy = CapacityPolicy()
        for cpu, ram, score, limit in ((2, 4, 1, 3), (4, 8, 2, 5),
                                       (8, 16, 4, 7)):
            with self.subTest(cpu=cpu, ram=ram):
                profile = ResourceProfile(cpu, ram)
                self.assertEqual(policy.score(profile), score)
                self.assertEqual(policy.active_limit(profile), limit)

    def test_cpu_and_ram_each_can_be_the_bottleneck(self):
        policy = CapacityPolicy()
        for profile in (ResourceProfile(2, 64), ResourceProfile(32, 4)):
            with self.subTest(profile=profile):
                self.assertEqual(policy.score(profile), 1)
                self.assertEqual(policy.active_limit(profile), 3)
        self.assertEqual(policy.score(ResourceProfile(8, 16)), 4)

    def test_half_up_rounding_and_neighboring_values(self):
        policy = CapacityPolicy()
        # These capacities put the unconstrained target just below, at, and
        # above 3.5. The tie goes upward rather than to an even integer.
        for capacity, expected in ((1.18, 3), (2 ** 0.25, 4), (1.20, 4),
                                   (2 ** 0.75, 5)):
            with self.subTest(capacity=capacity):
                self.assertEqual(policy.active_limit(ResourceProfile(
                    2 * capacity, 4 * capacity)), expected)

    def test_degree_limits_are_clipped_at_both_ends(self):
        policy = CapacityPolicy()
        self.assertEqual(policy.active_limit(ResourceProfile(0.02, 0.04)), 3)
        self.assertEqual(policy.active_limit(ResourceProfile(2048, 4096)), 9)
        self.assertEqual(policy.active_limit(ResourceProfile(1e-300, 1e-300)), 3)
        self.assertEqual(policy.active_limit(ResourceProfile(1e300, 1e300)), 9)

    def test_resource_values_must_be_positive_finite_numbers(self):
        for field in ("cpu", "ram_gib"):
            for value in (True, False, 0, -1, math.nan, math.inf, -math.inf,
                          "2", None):
                with self.subTest(field=field, value=value):
                    arguments = {"cpu": 2, "ram_gib": 4, field: value}
                    with self.assertRaises(ValueError):
                        ResourceProfile(**arguments)

    def test_apply_delegates_the_bound_change_to_the_target_node(self):
        class NodeSpy:
            def __init__(self):
                self.config = SimpleNamespace(active_size=5)
                self.calls = []

            def set_active_size(self, size):
                self.calls.append(size)
                self.config.active_size = size

        node = NodeSpy()
        self.assertEqual(apply_capacity(node, ResourceProfile(2, 4)), 3)
        self.assertEqual(node.calls, [3])
        self.assertEqual(node.config.active_size, 3)

    def test_apply_uses_the_supplied_policy(self):
        sim = Simulator()
        node = sim.add("a")
        policy = CapacityPolicy(min_degree=4, max_degree=6)
        self.assertEqual(apply_capacity(node, ResourceProfile(2, 4), policy), 4)
        self.assertEqual(node.config.active_size, 4)

    def test_unknown_registry_members_are_not_discovered_when_growing(self):
        sim = Simulator()
        a, b, c = [sim.add(i) for i in "abc"]
        self.assertEqual(apply_capacity(a, ResourceProfile(8, 16)), 7)
        sim.drain()
        self.assertEqual(a.config.active_size, 7)
        self.assertFalse(a.active | a.passive)
        self.assertEqual((b.config.active_size, c.config.active_size), (5, 5))
        self.assertFalse(b.active | c.active)
        sim.check()

    def test_growth_promotes_only_known_passive_candidates(self):
        sim = Simulator()
        a = sim.add("a", Config(active_size=3))
        b, c, unknown = [sim.add(i) for i in ("b", "c", "unknown")]
        a.connect("c", high=True)
        a.add_passive("b")
        self.assertEqual(apply_capacity(a, ResourceProfile(8, 16)), 7)
        sim.drain()
        self.assertEqual(a.active, {"b", "c"})
        self.assertEqual(b.active, {"a"})
        self.assertEqual(c.active, {"a"})
        self.assertFalse(unknown.active)
        self.assertNotIn("unknown", a.active | a.passive)
        self.assertNotIn("b", a.passive)
        sim.check()

    def test_shrink_preserves_symmetry_and_other_nodes_configuration(self):
        sim = Simulator(seed=4)
        a = sim.add("a")
        for node_id in "bcde":
            sim.add(node_id)
            a.connect(node_id, high=True)
        self.assertEqual(apply_capacity(a, ResourceProfile(2, 4)), 3)
        self.assertLessEqual(len(a.active), 3)
        sim.drain(max_events=1000)
        self.assertLessEqual(len(a.active), 3)
        self.assertEqual(a.config.active_size, 3)
        self.assertTrue(all(sim.nodes[i].config.active_size == 5 for i in "bcde"))
        sim.check()


class CapacitySuiteTests(unittest.TestCase):
    arguments = dict(nodes=12, seeds=(0,), warmup_rounds=1,
                     adaptation_rounds=1, broadcasts=2, recovery_rounds=1,
                     failure_fractions=(0, 0.5))

    @classmethod
    def setUpClass(cls):
        cls.result = run_capacity_suite(**cls.arguments)
        cls.dynamic = {run["variant"]: run for run in cls.result["dynamic_runs"]}

    @staticmethod
    def profiles(values):
        return {node_id: ResourceProfile(**profile)
                for node_id, profile in values.items()}

    def test_small_suite_is_reproducible_and_has_all_conditions(self):
        self.assertEqual(self.result, run_capacity_suite(**self.arguments))
        self.assertEqual(set(self.dynamic), {"fixed", "static", "permuted", "adaptive"})
        self.assertEqual(len(self.result["dynamic_runs"]), 4)
        self.assertEqual(len(self.result["failure_runs"]), 12)
        self.assertEqual(self.result["settings"]["nodes"], 12)
        self.assertEqual(self.result["settings"]["failure_detection"], "on_send")

    def test_static_and_adaptive_begin_with_identical_graph_and_workload(self):
        self.assertEqual(self.dynamic["static"]["before"],
                         self.dynamic["adaptive"]["before"])

    def test_invalid_population_and_failure_fractions_are_rejected(self):
        for override in ({"nodes": 6}, {"nodes": 13},
                         {"failure_fractions": (-0.1,)},
                         {"failure_fractions": (1,)},
                         {"failure_fractions": (math.nan,)},
                         {"failure_fractions": (True,)}):
            with self.subTest(arguments=override):
                with self.assertRaises(ValueError):
                    run_capacity_suite(**{**self.arguments, **override})

    def test_dynamic_variants_share_contacts_resources_and_event(self):
        reference = self.dynamic["fixed"]
        for run in self.dynamic.values():
            with self.subTest(variant=run["variant"]):
                self.assertEqual(run["seed"], 0)
                for field in ("contacts", "profiles_before", "profiles_after", "changed_nodes"):
                    self.assertEqual(run[field], reference[field])
                self.assertEqual(sum(run["limits_initial"].values()), 60)
                self.assertEqual(sum(run["limits_final"].values()), 60)
                self.assertTrue(run["history"])

    def test_resource_event_is_a_paired_swap_without_changing_population(self):
        run = self.dynamic["adaptive"]
        before, after = self.profiles(run["profiles_before"]), self.profiles(run["profiles_after"])
        fingerprint = lambda profiles: Counter((p.cpu, p.ram_gib) for p in profiles.values())
        self.assertEqual(fingerprint(before), fingerprint(after))
        upgraded = set(run["changed_nodes"]["upgraded"])
        downgraded = set(run["changed_nodes"]["downgraded"])
        self.assertTrue(upgraded)
        self.assertEqual(len(upgraded), len(downgraded))
        self.assertFalse(upgraded & downgraded)
        self.assertEqual({i for i in before if before[i] != after[i]}, upgraded | downgraded)
        policy = CapacityPolicy()
        for node_id in upgraded:
            self.assertGreater(policy.score(after[node_id]), policy.score(before[node_id]))
        for node_id in downgraded:
            self.assertLess(policy.score(after[node_id]), policy.score(before[node_id]))

    def test_static_limits_freeze_and_adaptive_limits_follow_new_resources(self):
        policy = CapacityPolicy()
        for variant in ("static", "adaptive"):
            run = self.dynamic[variant]
            expected_initial = {i: policy.active_limit(p)
                                for i, p in self.profiles(run["profiles_before"]).items()}
            self.assertEqual(run["limits_initial"], expected_initial)
        static, adaptive = self.dynamic["static"], self.dynamic["adaptive"]
        self.assertEqual(static["limits_final"], static["limits_initial"])
        expected_final = {i: policy.active_limit(p)
                          for i, p in self.profiles(adaptive["profiles_after"]).items()}
        self.assertEqual(adaptive["limits_final"], expected_final)
        self.assertNotEqual(adaptive["limits_initial"], adaptive["limits_final"])
        self.assertEqual(set(self.dynamic["fixed"]["limits_initial"].values()), {5})
        self.assertEqual(self.dynamic["fixed"]["limits_final"],
                         self.dynamic["fixed"]["limits_initial"])
        self.assertEqual(Counter(self.dynamic["permuted"]["limits_initial"].values()),
                         Counter(static["limits_initial"].values()))
        self.assertEqual(self.dynamic["permuted"]["limits_final"],
                         self.dynamic["permuted"]["limits_initial"])

    def test_failure_trials_pair_victims_origins_and_contacts(self):
        groups = defaultdict(list)
        for run in self.result["failure_runs"]:
            groups[(run["seed"], run["failure_mode"], run["failure_fraction"])].append(run)
        self.assertEqual(len(groups), 4)
        for condition, runs in groups.items():
            with self.subTest(condition=condition):
                self.assertEqual({run["variant"] for run in runs}, {"fixed", "static", "permuted"})
                reference = runs[0]
                for run in runs:
                    self.assertEqual(run["contacts"], self.dynamic["fixed"]["contacts"])
                    self.assertEqual(run["contacts"], reference["contacts"])
                    self.assertEqual(run["victims"], reference["victims"])
                    self.assertEqual([r["origin"] for r in run["broadcast_history"]],
                                     [r["origin"] for r in reference["broadcast_history"]])
                    self.assertEqual(len(run["victims"]), int(12 * run["failure_fraction"]))
                    self.assertEqual(len(run["broadcast_history"]), 2)
                    self.assertTrue(all(r["origin"] not in run["victims"]
                                        for r in run["broadcast_history"]))

    def test_high_capacity_failures_use_resources_rather_than_assigned_limits(self):
        profiles = self.profiles(self.dynamic["fixed"]["profiles_before"])
        policy = CapacityPolicy()
        for run in self.result["failure_runs"]:
            if run["failure_mode"] != "high_capacity" or not run["victims"]:
                continue
            victims = set(run["victims"])
            survivor_scores = [policy.score(profile) for i, profile in profiles.items()
                               if i not in victims]
            victim_scores = [policy.score(profiles[i]) for i in victims]
            self.assertGreaterEqual(min(victim_scores), max(survivor_scores))

    def test_no_membership_ticks_are_inserted_between_failure_broadcasts(self):
        for run in self.result["failure_runs"]:
            with self.subTest(variant=run["variant"], failure=run["failure_fraction"]):
                self.assertEqual(run["before"]["round"], 1)
                self.assertEqual(run["after_broadcasts"]["round"], 1)
                self.assertEqual(run["after_broadcasts"]["alive"], 12 - len(run["victims"]))

    def test_records_structure_hops_and_rejects_unknown_options(self):
        before = self.dynamic["static"]["before"]
        self.assertIn("capacity_degree_spearman", before["graph_properties"])
        self.assertGreaterEqual(before["window_mean_last_delivery_hops"], 0)
        for run in self.result["failure_runs"]:
            self.assertIn("articulation_points", run["after_broadcasts"]["graph_properties"])
        for override in ({"resource_mix": "unknown"}, {"failure_detection": "timeout"}):
            with self.subTest(arguments=override):
                with self.assertRaises(ValueError):
                    run_capacity_suite(**{**self.arguments, **override})

    def test_decoupled_mix_keeps_classes_and_budget_with_other_detector(self):
        result = run_capacity_suite(**{**self.arguments, "failure_fractions": (0,),
                                       "resource_mix": "decoupled",
                                       "failure_detection": "connection"})
        settings = result["settings"]
        self.assertEqual(settings["failure_detection"], "connection")
        self.assertEqual([(c["relative_capacity"], c["active_limit"])
                          for c in settings["resource_classes"]], [(1, 3), (2, 5), (4, 7)])
        static = next(r for r in result["dynamic_runs"] if r["variant"] == "static")
        self.assertEqual(static["before"]["active_limit_budget"], 60)
        self.assertIn({"cpu": 2, "ram_gib": 12}, static["profiles_before"].values())
        self.assertEqual(result["failure_runs"][0]["before"]["failure_detection"], "connection")


class GraphPropertyTests(unittest.TestCase):
    def test_star_hub_is_the_only_articulation_point(self):
        graph = {"h": {"a", "b", "c"}, "a": {"h"}, "b": {"h"}, "c": {"h"}}
        result = graph_properties(graph, {"h": 4, "a": 1, "b": 1, "c": 2})
        self.assertEqual(result["articulation_points"], 1)
        self.assertEqual(result["articulation_capacity_mean"], 4)
        self.assertEqual(result["bridges"], 3)
        # Every edge joins the hub to a leaf: perfectly disassortative.
        self.assertAlmostEqual(result["degree_assortativity"], -1)
        self.assertGreater(result["capacity_degree_spearman"], 0.7)
        self.assertEqual(result["capacity_mixing_edge_fraction"],
                         {"1-4": 2 / 3, "2-4": 1 / 3})

    def test_cycle_has_no_cut_and_undefined_correlations(self):
        graph = {"a": {"b", "d"}, "b": {"a", "c"}, "c": {"b", "d"}, "d": {"c", "a"}}
        result = graph_properties(graph, dict.fromkeys(graph, 1))
        self.assertEqual((result["articulation_points"], result["bridges"]), (0, 0))
        self.assertIsNone(result["capacity_degree_spearman"])
        self.assertIsNone(result["degree_assortativity"])

    def test_path_and_disconnected_parts_and_empty_graph(self):
        graph = {"a": {"b"}, "b": {"a", "c"}, "c": {"b"}, "x": {"y"}, "y": {"x"}, "z": set()}
        result = graph_properties(graph, dict.fromkeys(graph, 2))
        self.assertEqual((result["articulation_points"], result["bridges"]), (1, 3))
        empty = graph_properties({}, {})
        self.assertEqual((empty["articulation_points"], empty["capacity_mixing_edge_fraction"]),
                         (0, {}))


if __name__ == "__main__":
    unittest.main()
