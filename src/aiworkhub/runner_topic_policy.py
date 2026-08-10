from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# B119: runner/topic allowlist enforcement for write-gated taskctl actions.
#
# Deterministic safety boundary, NOT a neural learning target. It starts from
# the pinned runner/topic allowlist design evaluation and
# adds the exact ``claim-start`` lifecycle action. The neural controller may
# later choose among these identities for routing; this static allowlist is the
# hard boundary that blocks any identity not on the list.
#
# This layer only ever NARROWS an already-open AIWORKHUB_ALLOW_WRITES
# gate (see run_taskctl()) -- it never widens it and never runs when the
# write gate is closed. It only activates when a caller explicitly supplies
# a ``runner`` and/or ``topic`` identity. Exact lifecycle call sites always
# thread both values; legacy identity-free helpers such as export-jsonl retain
# their prior behavior.
# ---------------------------------------------------------------------------

# Runner/topic identity is created under the same canonical grammar used by
# task_create. Keeping a second underscore-only regex here made a card with a
# valid dotted or dashed topic creatable but permanently unlaunchable.
_RUNNER_TOPIC_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

# (runner, topic) -> allowed write_actions.
RUNNER_TOPIC_ALLOWLIST: dict[tuple[str, str], frozenset[str]] = {
    ("claude_coding", "coding"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_stem", "stem"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_general_reasoning", "general_reasoning"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_capability_eval", "capability_eval"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_context_graph", "context_graph"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_translate", "translation"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_c_native", "c_native"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_open_generation", "open_generation"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("deepseek_finance", "finance"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("deepseek_routing", "routing_safety"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("deepseek_tasking", "tasking_system"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("codex_spark_lexicon_multi_axis_enrichment_b367", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_real_generation_canary_b368", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_real_canary_shard_00_b368", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_real_canary_shard_01_b368", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_real_canary_shard_02_b368", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_real_canary_shard_03_b368", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_direct_micro_shard_00_b369", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_direct_micro_shard_01_b369", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_direct_micro_shard_02_b369", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_direct_micro_shard_03_b369", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_direct_micro_shard_00_b370", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_direct_micro_shard_01_b370", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_direct_micro_shard_02_b370", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_direct_micro_shard_03_b370", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_00_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_01_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_02_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_03_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_04_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_05_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_06_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_07_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_valency_shard_00_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_valency_shard_01_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_surface_shard_00_b380", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_surface_shard_01_b380", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_surface_apply_b381", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_surface_apply_b387", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_offline_pos_reconcile_b388", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_exact_repair_shard_00_b389", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_exact_repair_shard_01_b389", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_exact_repair_shard_02_b389", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_exact_repair_shard_03_b389", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_exact_selective_apply_b390", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_low_frame_repair_b391", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_applicability_repair_b392", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_offline_nonverb_reconcile_b393", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_null_pos_repair_b394", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_role_calibration_b395", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_role_calibration_b396", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_role_calibration_b397", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_recognizable_final_apply_b398", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_task_mcp_callback_bridge_finish_b384", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_task_mcp_callback_compact_b435", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_task_mcp_worker_ai_infra_b434", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_task_mcp_ai_infra_context_b437", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_task_mcp_ai_infra_canary_b439", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b438_residual_b440", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b438_residual_direct_s00_b441", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b440_partition_b442", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b441_partition_b443", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b438_residual_direct_s01_b444", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_task_mcp_vscode_dashboard_extension_b445", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b438_remaining_b446", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_b441_rework_b447", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b440_rework_b448", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b427_rework_b449", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b444_partition_b450", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_task_mcp_dashboard_exact_counts_b451", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_missing84_b452", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_00_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_01_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_02_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_03_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_04_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_05_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_06_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_07_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_08_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_09_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_10_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_11_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_12_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_13_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_14_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_15_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_16_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_17_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_18_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_19_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_20_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_21_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_22_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_23_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_24_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_25_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_26_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_27_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_28_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_29_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_30_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_31_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_00_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_02_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_03_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_05_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_06_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_08_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_09_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_11_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_12_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_14_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_15_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_17_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_18_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_20_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_21_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_23_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_24_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_26_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_27_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_29_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_30_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_00_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_01_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_02_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_03_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_04_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_05_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_06_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_07_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_08_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_09_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_10_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_11_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_12_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_13_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_14_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_15_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_00_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_01_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_02_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_03_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_04_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_05_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_06_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_07_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_08_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_09_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_10_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_11_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_12_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_13_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_14_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_15_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_b464_source_confirmed_lemma_apply_b465", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_b464_lemma_pair_adjudication_b466", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_b464_lemma_pair_adjudication_b466_v2", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_b464_shard12_lemma_adjudication_b467", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_b464_exact_lemma_adjudication_b468", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_morphology_residual_b469", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_source_heldout_b469", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_source_heldout_b469", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_morphology_selective_apply_b470", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_source_heldout_repair_b470", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_conditioned_precision_b470", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_task_mcp_multi_inprogress_callback_repair_b471", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_task_mcp_multi_instance_sideband_routing_b472", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_offline_exhaustion_b383", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_valency_surface_repair_b382", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_valency_conditioned_engine_b421", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_closure_semantic_canary_b373", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_canonical_semantic_apply_b373", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_closure_b377", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_canonical_enum_canary_b374", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_live_queue_rebase_b399", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_00_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_01_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_02_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_03_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_04_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_05_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_06_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_07_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_literal_chunk_00_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_literal_chunk_01_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_literal_chunk_02_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_literal_chunk_03_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_literal_chunk_04_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_literal_chunk_05_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_literal_chunk_06_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_literal_chunk_07_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_literal_selective_apply_b402", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_one_pass_complete_classification_b403", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_offline_full_axis_b410", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_compact_live_offline_b411", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_local_source_pack_repair_b413", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_consolidated_quarantine_b414", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_direct_b415_s00c00", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_b418", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_source_heldout_b419", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_source_heldout_b419", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_ud_glc_valency_heldout_b420", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_conditioned_case_set_apply_b422", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_final_b423", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s00", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s01", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s02", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s03", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s04", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s05", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s06", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s07", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_quarantine_selective_apply_b425", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_live_residual_rebase_b426", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_00_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_01_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_02_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_03_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_04_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_05_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_06_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_07_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b427_incremental_apply_b428", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b427_shards_02_03_apply_b429", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b427_shards_04_07_apply_b430", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s00", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s01", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s02", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s03", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s04", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s05", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s06", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s07", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_adjudication_canary_b432", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_adjudication_canary_b433", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_adjudication_canary_b433", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_adjudication_b436_s00", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_adjudication_b436_s01", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_adjudication_b440_s02", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_adjudication_b440_s03", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_morphology_b400_apply_b436", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_consensus_apply_b438", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_morphology_residual_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_source_heldout_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_b403_complete_selective_apply_builder_b408", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_source_heldout_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    # B473 exact runners.  Keep these explicit: Lexicon does not use a broad
    # per-wave prefix allowlist, so every launched worker remains auditable.
    ("codex_gpt55_lexicon_morphology_final_s00_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_morphology_final_s01_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_morphology_final_s02_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_morphology_final_s03_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_conditioned_coverage_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_heldout_family_a_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_heldout_family_b_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_sense_118_adjudication_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    # B474: consume the already-completed B464 classifications exactly once.
    # These workers normalize disjoint axis families; none may mutate the
    # canonical Lexicon or reopen ACCEPT_VALID/terminal dispositions.
    ("deepseek_v4pro_lexicon_b464_form_axis_normalize_b474", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_b464_semantic_axis_normalize_b474", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_b464_structural_axis_normalize_b474", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_b464_disposition_accounting_b474", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_heldout_expand_b474", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_heldout_expand_b474", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    # B475: exact failed-axis residual only. These eight runners consume the
    # non-overlapping semantic/ambiguity/valency shards left by the accepted
    # B474 rebase; broad Lexicon prefixes remain deliberately disallowed.
    ("codex_gpt55_lexicon_failed_axes_shard00_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_failed_axes_shard01_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_failed_axes_shard02_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_failed_axes_shard03_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_failed_axes_shard04_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_failed_axes_shard05_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_failed_axes_shard06_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_failed_axes_shard07_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_existing_m1_m9_real_e2e_b498", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_atlas_true_feature_selector_b501", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_atlas_production_elex_pgeo_b502", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_atlas_pgeo_producer_b504", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_complex_verifier_producers_b506", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_atlas_semantic_pgeo_b507", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_atlas_real_context_encoder_b509", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_blind_shadow_boundary_b510", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_task_mcp_finalized_worktree_gc_b511", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_task_mcp_zero_row_output_b536", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_blind_sae_adapter_b513", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_atlas_native_learned_pgeo_b514", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_blind_sae_adapter_b515", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_atlas_native_semantic_context_b516", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_blind_sae_zero_authority_b517", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_atlas_real_corpus_recurrent_b518", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_chatengine_zero_authority_shadow_b519", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_representation_parse_place_production_rebase_b520", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_extraction_repair_b521", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_representation_parse_place_genuine_lgp_rebuild_b522", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_operand_repair_b524", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_ramaz044_source_adjudication_b525", "context_graph"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_representation_parse_place_lgp_axes_native_b527", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_semantic_adjudication_b528", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_topup_b529", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_canonical_merge_b530", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_noncount_audit_b531", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_basis_ternary_b532", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_relational_negative_b533", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_rare_case_local_corpus_b534", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_observer_packet_repair_b535", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_owner_gate_consolidation_b537", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_multihop_leipzig_b538", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_contradiction_matsne_b538", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_contradiction_matsne_b538_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_contradiction_nplg_b539", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_contradiction_nplg_b539_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_highrecall_leipzig_news_b540", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_highrecall_leipzig_webwiki_b540", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_webwiki_adjudication_b541", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_news_adjudication_b541", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_opposition_leipzig_b542", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_opposition_unique_b543", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_owner_close_b544", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_owner_apply_preflight_b545", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s1_owner_apply_preflight_b545_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s1_final_owner_apply_b546", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s1_underdetermined_abstain_b547", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s0_abi_preflight_b548", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s0_abi_repair_b549", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s0_basis_relation_mapping_b550", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_qualitative_production_b551", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s1_entity_frame_inverse_repair_b552", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_exact_root_binding_b553", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s1_immutable_root_binding_b554", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s1_s0_abi_parity_b555", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s2_rigid_metric_kernel_b556", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s2_region_extent_sidecar_b557", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_explicit_metric_casebank_b558", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_casebank_adjudicate_materialize_b559", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s3_shape_sidecar_b560", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_task_mcp_required_ignored_output_promotion_b561", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s2_direct_semantic_adjudication_b562", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_accepted_materialization_b563", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s2_context_recovery_b564", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s3_native_consumer_b565", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_canonical_metric_bridge_b566", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s3_review_packet_b567", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_context_canonical_merge_b568", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s3_sentence_disjoint_adjudication_b569", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s2_blind_heldout_validation_b570", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s3_full_direct_repair_rebuild_b571", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s2_exact_three_repair_b572", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s4_motion_time_preflight_b573", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s5_signature128_preflight_b574", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s6_shadow_cuda_preflight_b575", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s2_canonical_transport_b576", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s3_figure_bound_repair_b577", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s5_signature128_truth_repair_b578", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s3_figure_direct_b579a", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s3_bound_direct_b579b", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s4_real_corpus_packet_b580", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_canonical_writer_repair_b581", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s3_canonical_v2_merge_b582", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s3_canonical_v2_merge_b582_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_candidate_direct_adjudication_b583a", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s4_blind_heldout_adjudication_b583b", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_native_abi_final_repair_b584", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_accepted22_grounding_b585", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s3_production_header_pack_b586", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_heldout21_outcome_b587", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_entity_frame_producer_b588", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s5_s2_adapter_b589", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s5_s2_adapter_b589_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s5_s3_adapter_b590", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_scaleout_even_b591", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s4_scaleout_odd_b592", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_scaleout_odd_b592_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s4_heldout_entity_frame_b593", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_combined_repair125_b594", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_b593_native_producer_repair_b595", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s4_b594_entity_frame_b596", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_b594_entity_frame_b596_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_b591_accept25_entity_frame_b597", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_canonical_s5_joined_b598", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_native_s5_joined_b599", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s6_real_gpu_closure_b600", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_representation_tensor_b601", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_representation_tensor_b601_v2", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_recurrent_completion_b602", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_s5_real_pair_sieve_b603", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_signal_atlas_hyperbolic_real_ablation_b604", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_complex_verifier_real_ablation_b605", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_complex_verifier_real_ablation_b605_v2", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_signal_atlas_s5_sieve_truth_repair_b606", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_hyperbolic_decision_producer_b607", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_complex_support_producer_b608", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_signal_atlas_verifier_unguarded_producer_b609", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_real_executable_shadow_b610", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_s5_sieve_native_repair_b611", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_s5_crosswalk_native_repair_b612", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_s5_crosswalk_native_repair_b612", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_root_signal_nonempty_canary_b613", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_task_mcp_b614_repair_b616", "tasking_system"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_gate17_hyperbolic_paired_ablation_b617", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_gate17_complex_paired_ablation_b618", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_signal_atlas_gate17_verifier_paired_ablation_b619", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_canonical_sae_runtime_b620", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_b613_query_packet_producer_b621", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_runtime_root_reconciliation_b622", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_runtime_root_reconciliation_b622_v2", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_runtime_root_reconciliation_b622_v3", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_runtime_root_reconciliation_b622_v4", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_task_mcp_current_tree_policy_repair_b623", "tasking_system"): frozenset({"claim-start", "review", "usage"}),
}

# codex is the coordinator: matches ANY topic (topic="*" in the design
# matrix) with a distinct action set. Codex never auto-picks-up or starts
# worker tasks; it finalizes (done), exports, reviews, and records usage.
CODEX_RUNNER = "codex"
CODEX_ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {
        "archive",
        "done",
        "export-jsonl",
        "restore",
        "review",
        "reject-review",
        "recover-blocked-rework",
        "release-launch",
        "retry-terminal",
        "usage",
    }
)

# ---------------------------------------------------------------------------
# B06: bounded per-wave task_mcp prefix allowlist (applies the pinned B05
# dry-run proposal).
#
# This adds one topic key, ``task_mcp``, matched by a runner name prefix instead
# of an exact tuple. Task MCP runners are per-wave-numbered (e.g.
# ``claude_task_mcp_allowlist_patch_b06``) and cannot be enumerated in
# advance. Still a deterministic safety/support-boundary gate (CLAUDE.md
# carve-out), not a learned routing decision -- no regex/keyword cognition is
# introduced for task-type or route selection, only a widened prefix check on
# an already-static safety boundary. Exact ``claim-start`` is allowed;
# standalone ``start`` and coordinator-only ``done`` remain excluded.
# ---------------------------------------------------------------------------

PER_WAVE_RUNNER_TOPIC_ALLOWLIST: dict[str, dict[str, Any]] = {
    "task_mcp": {
        "runner_prefix": "claude_task_mcp_",
        "actions": frozenset({"auto-pickup", "claim-start", "review", "usage"}),
    },
}


def _is_malformed_identity_token(value: str) -> str | None:
    """Return a short reason string if ``value`` fails sanity checks, else None.

    Rejects: empty string, embedded null bytes, and anything outside the
    canonical ``[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`` identity grammar (this
    rejects whitespace, path separators/traversal, and shell metacharacters
    like ``; | & $`` in one check). Matching is exact/case-sensitive against ``RUNNER_TOPIC_ALLOWLIST``
    keys -- no case normalization is performed anywhere in this module, so a
    case-mismatched runner (e.g. ``CLAUDE_CODING``) passes this sanity check
    but is then rejected as an unknown pair by the allowlist lookup.
    """
    if value == "":
        return "empty_string"
    if "\x00" in value:
        return "null_byte"
    if not _RUNNER_TOPIC_TOKEN_RE.match(value):
        return "invalid_characters"
    return None


def check_runner_topic_allowlist(
    runner: str | None,
    topic: str | None,
    action: str,
) -> dict[str, Any]:
    """Pure allowlist decision for a (runner, topic, action) write triple.

    Returns ``{"allowed": bool, "reason": str}``. Never raises, never performs
    I/O, never mutates state, never launches a process. This is the
    deterministic safety gate from mcp_runner_topic_allowlist_design_b118_v1.json
    (it is explicitly NOT a neural learning target -- see module note above).
    """
    if runner is not None:
        runner_reason = _is_malformed_identity_token(runner)
        if runner_reason:
            return {"allowed": False, "reason": f"malformed_runner:{runner_reason}"}
    if topic is not None:
        topic_reason = _is_malformed_identity_token(topic)
        if topic_reason:
            return {"allowed": False, "reason": f"malformed_topic:{topic_reason}"}

    if runner == CODEX_RUNNER:
        if action in CODEX_ALLOWED_ACTIONS:
            return {"allowed": True, "reason": "codex_wildcard_topic_allowed"}
        return {"allowed": False, "reason": f"codex_action_not_allowed:{action}"}

    if runner is None or topic is None:
        return {"allowed": False, "reason": "runner_and_topic_required_for_non_codex"}

    allowed_actions = RUNNER_TOPIC_ALLOWLIST.get((runner, topic))
    if allowed_actions is not None:
        if action not in allowed_actions:
            return {"allowed": False, "reason": f"action_not_allowed_for_runner_topic:{action}"}
        return {"allowed": True, "reason": "allowlisted"}

    per_wave_rule = PER_WAVE_RUNNER_TOPIC_ALLOWLIST.get(topic)
    if per_wave_rule is not None and runner.startswith(per_wave_rule["runner_prefix"]):
        if action not in per_wave_rule["actions"]:
            return {"allowed": False, "reason": f"per_wave_action_not_allowed:{action}"}
        return {"allowed": True, "reason": "per_wave_prefix_allowlisted"}

    return {"allowed": False, "reason": "unknown_runner_topic_pair"}

