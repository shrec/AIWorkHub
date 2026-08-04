"""Tests for honest context economics (B812_v1).

Uses stdlib unittest — no external deps.  Coverage:
- bundle larger than selected sections (overhead)
- absent baseline (null / not_measured)
- cached-token subset semantics (OpenAI)
- Claude disjoint cache fields
- failed task cost
- multi-repo separation
"""

from __future__ import annotations

import json
import math
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from aiworkhub.context_economics import (  # noqa: E402
    aggregate_context_economics,
    compare_policies,
    dashboard_record,
    measure_context_delivery,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _section(name: str, content: str) -> dict:
    return {
        "name": name,
        "bytes": len(content.encode("utf-8")),
        "sha256": "fake",
        "truncated": False,
        "degraded_reason": "",
        "requested": True,
        "executed": True,
        "hit_count": 1,
    }


SRC = json.dumps({"results": [{"id": 1, "text": "hello" * 100}]})
SESSION = json.dumps({"state": "ok", "count": 5})
KB = "[kb] found 3 results\n"


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestBundleOverhead(unittest.TestCase):
    """1. Bundle larger than selected sections (overhead test)."""

    def test_overhead_computed(self):
        sections = [
            _section("source_graph", SRC),
            _section("session_current_state", SESSION),
            _section("kb", KB),
        ]
        selected = sum(s["bytes"] for s in sections)
        bundle_bytes = selected + 512

        result = measure_context_delivery(
            project_context_metadata={
                "sections": sections,
                "bundle_bytes": bundle_bytes,
            },
            task_id="T01",
        )

        pops = result["populations"]
        self.assertEqual(pops["selected_source_bytes"], selected)
        self.assertEqual(pops["delivered_prompt_bytes"], bundle_bytes)
        self.assertEqual(pops["serialized_envelope_bytes"], 512)
        self.assertAlmostEqual(
            result["ratios"]["envelope_overhead_ratio"],
            512 / bundle_bytes,
            places=4,
        )


class TestAbsentBaseline(unittest.TestCase):
    """2. Absent baseline (null / not_measured)."""

    def test_naive_null_stays_null(self):
        sections = [_section("source_graph", SRC)]
        result = measure_context_delivery(
            project_context_metadata={
                "sections": sections,
                "bundle_bytes": len(SRC.encode("utf-8")),
            },
            naive_discover_bytes=None,
            task_id="T02",
        )
        self.assertIsNone(result["populations"]["naive_discover_bytes"])
        self.assertIsNone(
            result["ratios"]["compression_ratio_vs_naive_discover"]
        )
        self.assertTrue(result["notes"]["null_means_not_measured"])

    def test_all_null_when_no_evidence(self):
        result = measure_context_delivery(task_id="T03")
        pops = result["populations"]
        self.assertIsNone(pops["naive_discover_bytes"])
        self.assertIsNone(pops["selected_source_bytes"])
        self.assertIsNone(pops["serialized_envelope_bytes"])
        self.assertIsNone(pops["delivered_prompt_bytes"])
        self.assertIsNone(pops["cached_input_tokens"])
        self.assertIsNone(pops["model_input_tokens"])
        self.assertIsNone(pops["model_output_tokens"])
        self.assertIsNone(pops["cost_usd"])


class TestPromptEncodingCounterfactual(unittest.TestCase):
    """Exact same-evidence v1/v2 byte accounting stays separate from tokens."""

    def test_nested_encoding_delta_is_measured(self):
        sections = [_section("source_graph", SRC)]
        result = measure_context_delivery(
            project_context_metadata={
                "sections": sections,
                "bundle_bytes": 800,
                "optimization": {
                    "prompt_encoding": {
                        "legacy_v1_bundle_bytes": 1000,
                        "nested_v2_bundle_bytes": 800,
                    }
                },
            },
            task_id="T03-encoding",
        )
        pops = result["populations"]
        self.assertEqual(pops["legacy_v1_prompt_bytes"], 1000)
        self.assertEqual(pops["prompt_encoding_delta_bytes"], -200)
        self.assertEqual(result["ratios"]["prompt_encoding_reduction"], 0.2)

        bucket = aggregate_context_economics([result])["by_repo"]["unknown"]
        self.assertEqual(bucket["encoding_observed_tasks"], 1)
        self.assertEqual(bucket["total_legacy_v1_prompt_bytes"], 1000)
        self.assertEqual(bucket["total_nested_v2_prompt_bytes"], 800)
        self.assertEqual(bucket["total_prompt_encoding_delta_bytes"], -200)


class TestOpenAICacheSubset(unittest.TestCase):
    """3. Cached-token subset semantics (OpenAI shape)."""

    def test_openai_cached_is_subset(self):
        result = measure_context_delivery(
            usage={
                "input_tokens": 10000,
                "output_tokens": 500,
                "cached_input_tokens": 4000,
                "cost_usd": 0.05,
            },
            adapter_id="deepseek_v4_pro",
            task_id="T04",
        )
        pops = result["populations"]
        self.assertEqual(pops["model_input_tokens"], 10000)
        self.assertEqual(pops["cached_input_tokens"], 4000)
        self.assertAlmostEqual(
            result["ratios"]["cache_hit_rate_of_input"], 0.4
        )
        self.assertTrue(result["cache_semantics"]["cache_subset_of_input_tokens"])
        self.assertEqual(
            result["cache_semantics"]["provider_shape"], "openai_subset"
        )

    def test_openai_observed_zero_cache_is_zero(self):
        result = measure_context_delivery(
            usage={"input_tokens": 5000, "cached_input_tokens": 0},
            adapter_id="deepseek_v4_pro",
            task_id="T05",
        )
        self.assertEqual(result["populations"]["cached_input_tokens"], 0)
        self.assertEqual(
            result["populations"]["cache_eligible_input_tokens"], 5000
        )
        self.assertEqual(result["ratios"]["cache_hit_rate_of_input"], 0.0)


class TestClaudeDisjointCache(unittest.TestCase):
    """4. Claude disjoint cache fields."""

    def test_claude_cache_read_is_hit_and_creation_is_not(self):
        result = measure_context_delivery(
            usage={
                "input_tokens": 8000,
                "output_tokens": 300,
                "cache_read_input_tokens": 3000,
                "cache_creation_input_tokens": 500,
                "cost_usd": 0.12,
            },
            adapter_id="claude_cli",
            task_id="T06",
        )
        pops = result["populations"]
        self.assertEqual(pops["cached_input_tokens"], 3000)
        self.assertEqual(pops["cache_creation_input_tokens"], 500)
        self.assertEqual(pops["cache_eligible_input_tokens"], 11500)
        self.assertEqual(
            result["cache_semantics"]["provider_shape"], "claude_disjoint"
        )
        self.assertFalse(
            result["cache_semantics"]["cache_subset_of_input_tokens"]
        )
        self.assertAlmostEqual(
            result["ratios"]["cache_hit_rate_of_input"], 3000 / 11500,
            places=4,
        )

    def test_claude_launcher_cached_field_is_accepted_as_cache_read(self):
        result = measure_context_delivery(
            usage={
                "input_tokens": 8000,
                "cached_input_tokens": 3000,
                "cache_creation_input_tokens": 500,
            },
            adapter_id="claude_cli",
            task_id="T06-launcher",
        )
        self.assertEqual(result["populations"]["cached_input_tokens"], 3000)
        self.assertEqual(
            result["populations"]["cache_eligible_input_tokens"], 11500
        )

    def test_impossible_openai_cache_ratio_is_invalid_not_clamped(self):
        result = measure_context_delivery(
            usage={"input_tokens": 100, "cached_input_tokens": 200},
            adapter_id="deepseek_v4_pro",
            task_id="T06-invalid",
        )
        self.assertFalse(result["populations"]["cache_metric_valid"])
        self.assertIsNone(result["ratios"]["cache_hit_rate_of_input"])
        self.assertEqual(
            result["cache_semantics"]["invalid_reason"],
            "cached_tokens_exceed_eligible_input",
        )

    def test_claude_no_cache_null(self):
        result = measure_context_delivery(
            usage={"input_tokens": 1000},
            adapter_id="claude_cli",
            task_id="T07",
        )
        self.assertIsNone(result["populations"]["cached_input_tokens"])
        self.assertIsNone(
            result["populations"]["cache_creation_input_tokens"]
        )


class TestFailedTaskCost(unittest.TestCase):
    """5. Failed task cost."""

    def test_failed_cost_tracked(self):
        result = measure_context_delivery(
            usage={
                "input_tokens": 2000,
                "output_tokens": 100,
                "cost_usd": 0.03,
            },
            outcome="failed",
            task_id="T08",
            repo_id="repo_a",
        )
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["populations"]["cost_usd"], 0.03)

        agg = aggregate_context_economics([result])
        self.assertEqual(agg["task_totals"]["total_tasks"], 1)
        self.assertEqual(agg["task_totals"]["accepted_tasks"], 0)
        self.assertEqual(agg["cost_totals"]["total_cost_usd"], 0.03)

    def test_no_accepted_div_zero_handled(self):
        m = measure_context_delivery(
            usage={"input_tokens": 100, "cost_usd": 0.01},
            outcome="failed",
            task_id="T09",
        )
        agg = aggregate_context_economics([m])
        self.assertIsNone(agg["derived"]["cost_per_accepted_task_usd"])
        self.assertIsNone(agg["derived"]["cost_per_review_ready_task_usd"])


class TestMultiRepo(unittest.TestCase):
    """6. Multi-repo separation."""

    def test_separate_buckets(self):
        m_a = measure_context_delivery(
            usage={"input_tokens": 1000, "cost_usd": 0.01},
            repo_id="geoai",
            task_id="T10",
        )
        m_b = measure_context_delivery(
            usage={"input_tokens": 2000, "cost_usd": 0.02},
            repo_id="task_mcp",
            task_id="T11",
        )
        agg = aggregate_context_economics([m_a, m_b])
        repos = agg["by_repo"]
        self.assertEqual(repos["geoai"]["task_count"], 1)
        self.assertEqual(repos["geoai"]["total_cost_usd"], 0.01)
        self.assertEqual(repos["task_mcp"]["task_count"], 1)
        self.assertEqual(repos["task_mcp"]["total_cost_usd"], 0.02)
        self.assertEqual(agg["task_totals"]["total_tasks"], 2)
        self.assertEqual(agg["cost_totals"]["total_cost_usd"], 0.03)

    def test_accepted_per_repo(self):
        m_a = measure_context_delivery(
            usage={"input_tokens": 500, "cost_usd": 0.005},
            repo_id="geoai",
            outcome="accepted",
            task_id="T12",
        )
        m_b = measure_context_delivery(
            usage={"input_tokens": 300, "cost_usd": 0.002},
            repo_id="task_mcp",
            outcome="failed",
            task_id="T13",
        )
        agg = aggregate_context_economics([m_a, m_b])
        self.assertEqual(agg["task_totals"]["accepted_tasks"], 1)
        self.assertEqual(agg["derived"]["cost_per_accepted_task_usd"], 0.007)
        self.assertEqual(agg["cost_totals"]["total_cost_usd"], 0.007)


class TestDashboardRecord(unittest.TestCase):
    """7. Dashboard record shape."""

    def test_shape_and_flags(self):
        m = measure_context_delivery(
            usage={"input_tokens": 100, "cost_usd": 0.001},
            outcome="accepted",
            task_id="T14",
            adapter_id="claude_cli",
        )
        rec = dashboard_record([m])
        self.assertEqual(
            rec["schema_id"], "aiworkhub.task_mcp.context_economics.v1"
        )
        self.assertEqual(rec["record_type"], "dashboard_ready")
        self.assertEqual(rec["summary"]["total_tasks"], 1)
        self.assertEqual(rec["summary"]["accepted_tasks"], 1)
        self.assertEqual(rec["summary"]["cost_per_accepted_usd"], 0.001)
        self.assertFalse(rec["authority_flags"]["dashboard_mutation"])
        self.assertFalse(rec["authority_flags"]["runtime_authority"])


class TestComparePolicies(unittest.TestCase):
    """8. compare_policies."""

    def test_before_after_delta(self):
        before = [
            measure_context_delivery(
                usage={"input_tokens": 10000, "cost_usd": 0.10},
                outcome="accepted",
                task_id="old_1",
            ),
        ]
        after = [
            measure_context_delivery(
                usage={"input_tokens": 4000, "cost_usd": 0.03},
                outcome="accepted",
                task_id="new_1",
            ),
        ]
        cmp = compare_policies(
            label="sg_v2_vs_v1",
            before_measurements=before,
            after_measurements=after,
        )
        self.assertEqual(cmp["comparison_label"], "sg_v2_vs_v1")
        self.assertEqual(cmp["delta"]["cost_usd_delta"], -0.07)
        self.assertEqual(cmp["delta"]["cost_usd_change_pct"], -70.0)
        self.assertEqual(cmp["delta"]["accepted_tasks_delta"], 0)

    def test_before_zero_cost_pct_null(self):
        before = [
            measure_context_delivery(
                usage={"input_tokens": 100, "cost_usd": 0.0},
                outcome="accepted",
                task_id="z1",
            ),
        ]
        after = [
            measure_context_delivery(
                usage={"input_tokens": 200, "cost_usd": 0.01},
                outcome="accepted",
                task_id="z2",
            ),
        ]
        cmp = compare_policies(
            label="zero_baseline",
            before_measurements=before,
            after_measurements=after,
        )
        self.assertEqual(cmp["delta"]["cost_usd_delta"], 0.01)
        self.assertIsNone(cmp["delta"]["cost_usd_change_pct"])


class TestNegativeSavings(unittest.TestCase):
    """9. Negative savings reported honestly."""

    def test_negative_savings(self):
        before = [
            measure_context_delivery(
                usage={"input_tokens": 1000, "cost_usd": 0.01},
                outcome="accepted",
                task_id="neg1",
            ),
        ]
        after = [
            measure_context_delivery(
                usage={"input_tokens": 5000, "cost_usd": 0.05},
                outcome="accepted",
                task_id="neg2",
            ),
        ]
        cmp = compare_policies(
            label="regression",
            before_measurements=before,
            after_measurements=after,
        )
        self.assertEqual(cmp["delta"]["cost_usd_delta"], 0.04)
        self.assertEqual(cmp["delta"]["cost_usd_change_pct"], 400.0)


class TestAggregationAllDimensions(unittest.TestCase):
    """10. Aggregation by task_type, adapter, outcome, runner."""

    def test_all_dimensions(self):
        ms = []
        for i in range(3):
            ms.append(
                measure_context_delivery(
                    usage={"input_tokens": 100, "cost_usd": 0.001},
                    repo_id="r1",
                    task_type="code",
                    adapter_id="claude_cli",
                    outcome="accepted",
                    runner="claude_coding",
                    task_id=f"dim{i}",
                )
            )
        ms.append(
            measure_context_delivery(
                usage={"input_tokens": 200, "cost_usd": 0.002},
                repo_id="r2",
                task_type="research",
                adapter_id="deepseek_v4_pro",
                outcome="failed",
                runner="deepseek_research",
                task_id="dim_research",
            )
        )
        agg = aggregate_context_economics(ms)

        self.assertEqual(agg["by_repo"]["r1"]["task_count"], 3)
        self.assertEqual(agg["by_repo"]["r2"]["task_count"], 1)
        self.assertEqual(agg["by_task_type"]["code"]["task_count"], 3)
        self.assertEqual(agg["by_task_type"]["research"]["task_count"], 1)
        self.assertEqual(agg["by_adapter"]["claude_cli"]["task_count"], 3)
        self.assertEqual(
            agg["by_adapter"]["deepseek_v4_pro"]["task_count"], 1
        )
        self.assertEqual(agg["by_outcome"]["accepted"]["task_count"], 3)
        self.assertEqual(agg["by_outcome"]["failed"]["task_count"], 1)
        self.assertEqual(agg["by_runner"]["claude_coding"]["task_count"], 3)
        self.assertEqual(
            agg["by_runner"]["deepseek_research"]["task_count"], 1
        )
        self.assertEqual(agg["cost_totals"]["total_cost_usd"], 0.005)
        self.assertEqual(agg["cost_totals"]["total_input_tokens"], 500)
        self.assertAlmostEqual(
            agg["derived"]["cost_per_accepted_task_usd"],
            round(0.005 / 3, 6),
            places=6,
        )


class TestByteDisclaimer(unittest.TestCase):
    """11. Byte values never presented as token/dollar truth."""

    def test_disclaimers_present(self):
        result = measure_context_delivery(
            project_context_metadata={
                "sections": [_section("src", "x" * 100)],
                "bundle_bytes": 200,
            },
            naive_discover_bytes=10000,
            task_id="T15",
        )
        notes = result["notes"]
        self.assertIn("byte_labels", notes)
        self.assertIn("NOT token", notes["byte_labels"])
        self.assertTrue(notes["null_means_not_measured"])
        self.assertTrue(notes["no_fabricated_10x_claims"])
        self.assertEqual(
            result["ratios"]["compression_ratio_vs_naive_discover"], 100.0
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
