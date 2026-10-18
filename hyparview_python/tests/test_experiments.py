import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from experiments import aggregate_runs, estimate, main, run_experiment, run_suite
from hyparview import Config


class ExperimentTests(unittest.TestCase):
    def test_summary_uncertainty_and_single_run(self):
        self.assertEqual(estimate([])["n"], 0)
        self.assertIsNone(estimate([1])["ci95"])
        result = estimate([1, 2, 3])
        self.assertEqual(result["mean"], 2)
        self.assertEqual(result["stdev"], 1)
        self.assertAlmostEqual(result["ci95"][0], 2 - 1.96 / 3**0.5)

    def test_experiment_reproducible_and_no_interleaved_ticks(self):
        kwargs = dict(nodes=20, seed=3, failure_fraction=0.5,
                      warmup_rounds=2, broadcasts=4, path_samples=3)
        first = run_experiment(**kwargs)
        self.assertEqual(first, run_experiment(**kwargs))
        self.assertEqual(first["failure_count"], 10)
        self.assertEqual(len(first["broadcast_history"]), 4)
        self.assertEqual(first["after_join"]["round"], 0)
        for phase in ("before_failure", "immediately_after_failure", "after_broadcasts"):
            self.assertEqual(first[phase]["round"], 2)
        self.assertEqual(first["config"]["active_size"], 5)
        self.assertEqual(first["active_limit_budget"], 100)
        self.assertTrue(all(record["origin"] not in first["victims"]
                            for record in first["broadcast_history"]))

    def test_recovery_ticks_only_follow_broadcasts(self):
        result = run_experiment(nodes=10, warmup_rounds=1, broadcasts=2,
                                recovery_rounds=3, path_samples=None)
        self.assertEqual(result["after_broadcasts"]["round"], 1)
        self.assertEqual(result["after_recovery"]["round"], 4)
        self.assertEqual([snapshot["recovery_round"]
                          for snapshot in result["recovery_history"]], [0, 1, 2, 3])

    def test_paired_faults_and_origins_across_configuration(self):
        kwargs = dict(nodes=15, seed=2, contact_mode="random", warmup_rounds=2,
                      broadcasts=3, failure_fraction=0.4, path_samples=2)
        first = run_experiment(config=Config(active_size=3), **kwargs)
        second = run_experiment(config=Config(active_size=5), **kwargs)
        self.assertEqual(first["contacts"], second["contacts"])
        self.assertEqual(first["victims"], second["victims"])
        self.assertEqual([r["origin"] for r in first["broadcast_history"]],
                         [r["origin"] for r in second["broadcast_history"]])

    def test_suite_groups_seeds_and_preserves_each_run(self):
        result = run_suite(node_counts=[8, 10], seeds=[0, 1],
                           failure_fractions=[0, 0.5], warmup_rounds=1,
                           broadcasts=2, path_samples=2)
        self.assertEqual(len(result["runs"]), 8)
        self.assertEqual(len(result["aggregates"]), 4)
        for aggregate in result["aggregates"]:
            self.assertEqual(aggregate["run_count"], 2)
            self.assertEqual(aggregate["seeds"], [0, 1])
            self.assertEqual(aggregate["broadcast_history"][0]["fraction"]["n"], 2)
            self.assertEqual(aggregate["recovered_run_count"]
                             + aggregate["unrecovered_run_count"], 2)

    def test_connection_failure_callbacks_run_without_membership_tick(self):
        result = run_experiment(nodes=20, seed=3, failure_fraction=0.5,
                                warmup_rounds=2, broadcasts=1,
                                failure_detection="connection", path_samples=3)
        self.assertGreater(result["immediately_after_failure"]["stale_active_entries"], 0)
        self.assertEqual(result["after_broadcasts"]["stale_active_entries"], 0)
        self.assertEqual(result["after_broadcasts"]["round"], 2)
        self.assertEqual(result["failure_detection"], "connection")

    def test_detector_choices_preserve_paired_trials_and_group_separately(self):
        kwargs = dict(nodes=15, seed=2, contact_mode="random", warmup_rounds=2,
                      broadcasts=3, failure_fraction=0.4, path_samples=2)
        first = run_experiment(failure_detection="on_send", **kwargs)
        second = run_experiment(failure_detection="connection", **kwargs)
        self.assertEqual(first["contacts"], second["contacts"])
        self.assertEqual(first["victims"], second["victims"])
        self.assertEqual([r["origin"] for r in first["broadcast_history"]],
                         [r["origin"] for r in second["broadcast_history"]])
        self.assertEqual(len(aggregate_runs([first, second])), 2)

    def test_aggregation_separates_configurations_and_rejects_duplicate_seeds(self):
        kwargs = dict(nodes=8, warmup_rounds=0, broadcasts=1, path_samples=2)
        first = run_experiment(config=Config(active_size=3), **kwargs)
        second = run_experiment(config=Config(active_size=5), **kwargs)
        self.assertEqual(len(aggregate_runs([first, second])), 2)
        with self.assertRaises(ValueError):
            aggregate_runs([first, first])

    def test_aggregation_separates_different_broadcast_windows(self):
        kwargs = dict(nodes=8, warmup_rounds=0, path_samples=2)
        first = run_experiment(broadcasts=1, **kwargs)
        second = run_experiment(broadcasts=2, **kwargs)
        groups = aggregate_runs([first, second])
        self.assertEqual(len(groups), 2)
        self.assertEqual(sorted(len(group["broadcast_history"]) for group in groups), [1, 2])

    def test_invalid_experimental_inputs(self):
        for kwargs in ({"nodes": 1}, {"nodes": 2.5}, {"seed": -1},
                       {"failure_fraction": 1}, {"failure_fraction": float("nan")},
                       {"failure_fraction": True}, {"warmup_rounds": -1},
                       {"broadcasts": 0}, {"recovery_rounds": -1},
                       {"contact_mode": "typo"}, {"path_samples": 0},
                       {"failure_detection": "typo"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                run_experiment(**kwargs)
        for kwargs in ({"seeds": [0, 0]}, {"node_counts": []},
                       {"failure_fractions": [0.2, 0.2]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                run_suite(**kwargs)

    def test_cli_writes_full_json_and_concise_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results" / "test.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main(["--nodes", "8", "--seeds", "1,2",
                                       "--failure-fractions", "0.25",
                                       "--rounds", "1", "--broadcasts", "2",
                                       "--path-samples", "exact",
                                       "--failure-detector", "connection",
                                       "--output", str(output)]), 0)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(result["runs"]), 2)
            self.assertEqual(result["runs"][0]["failure_detection"], "connection")
            self.assertEqual(json.loads(stdout.getvalue())["runs"], 2)

    def test_cli_rejects_empty_or_duplicate_csv_values(self):
        for args in (["--seeds", "1,1"], ["--nodes", "8,"],
                     ["--path-samples", "0"], ["--failure-fractions", "nan"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
