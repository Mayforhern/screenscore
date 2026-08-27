"""Tests for the screenscore system prompt."""
from screenscore.prompt import (
    ADAPTIVE_QUERY_RECOVERY,
    DIRECTOR_RULES,
    EVIDENCE_PROVENANCE,
    MODEL_QUOTA_HANDLING,
    PERSONA,
    PIPELINE_STATUS,
    QUERY_METADATA_LOGGING,
    STEP_1_SCHEMA,
    STEP_3_PLAN,
    STEP_8_DECIDE,
    SYSTEM_PROMPT,
    TERMINAL_CONDITIONS,
)


def test_system_prompt_contains_persona():
    assert "ScreenScore" in SYSTEM_PROMPT
    assert "ClickHouse" in SYSTEM_PROMPT


def test_system_prompt_contains_pipeline_steps():
    assert "STEP 1 — SCHEMA" in SYSTEM_PROMPT
    assert "STEP 2 — DISCOVER" in SYSTEM_PROMPT
    assert "STEP 3 — PLAN" in SYSTEM_PROMPT
    assert "STEP 4 — QUERIES" in SYSTEM_PROMPT
    assert "STEP 5 — ANALYZE" in SYSTEM_PROMPT
    assert "STEP 6 — SYNTHETIC MARKET COMPS" in SYSTEM_PROMPT
    assert "STEP 7 — CONSTRAINT VALIDATION" in SYSTEM_PROMPT
    assert "STEP 8 — DECIDE" in SYSTEM_PROMPT


def test_system_prompt_contains_director_rules():
    assert DIRECTOR_RULES in SYSTEM_PROMPT
    assert "DQ1" in SYSTEM_PROMPT
    assert "DQ8" in SYSTEM_PROMPT


def test_system_prompt_contains_tool_failure_handling():
    assert "TOOL FAILURE HANDLING" in SYSTEM_PROMPT
    assert "STATUS: STEP FAILED" in SYSTEM_PROMPT


def test_system_prompt_contains_unavailable_fields():
    assert "vote_count" in SYSTEM_PROMPT
    assert "runtime" in SYSTEM_PROMPT
    assert "budget" in SYSTEM_PROMPT


def test_system_prompt_labels_required():
    assert "[ClickHouse IMDb]" in SYSTEM_PROMPT
    assert "[Synthetic Benchmark]" in SYSTEM_PROMPT
    assert "[User-provided]" in SYSTEM_PROMPT
    assert "[Unavailable]" in SYSTEM_PROMPT


def test_persona_section():
    assert "Acquisition Analyst" in PERSONA


def test_step_1_section():
    assert "get_schema_info" in STEP_1_SCHEMA


def test_step_8_section():
    assert "generate_acquisition_memo" in STEP_8_DECIDE
    assert "FURTHER_REVIEW" in STEP_8_DECIDE


def test_system_prompt_has_pipeline_header():
    assert "STRICT EXECUTION ORDER" in SYSTEM_PROMPT


def test_system_prompt_contains_adaptive_recovery():
    assert ADAPTIVE_QUERY_RECOVERY in SYSTEM_PROMPT
    assert "plan_follow_up_queries" in ADAPTIVE_QUERY_RECOVERY
    assert "diagnose_query_failure" in ADAPTIVE_QUERY_RECOVERY
    assert "STEP 4b" in SYSTEM_PROMPT
    assert "ADAPTIVE QUERY RECOVERY" in SYSTEM_PROMPT
    assert "EXHAUSTED ALL RECOVERY STRATEGIES" in SYSTEM_PROMPT


def test_system_prompt_contains_query_metadata_logging():
    assert QUERY_METADATA_LOGGING in SYSTEM_PROMPT
    assert "log_query_metadata" in QUERY_METADATA_LOGGING
    assert "query_id" in QUERY_METADATA_LOGGING
    assert "rows_returned" in QUERY_METADATA_LOGGING


def test_system_prompt_contains_pipeline_status():
    assert PIPELINE_STATUS in SYSTEM_PROMPT
    assert "update_step" in PIPELINE_STATUS
    assert "get_pipeline_status" in PIPELINE_STATUS
    assert "check_terminal_conditions" in PIPELINE_STATUS


def test_system_prompt_contains_html_memo_instruction():
    assert "generate_html_memo" in SYSTEM_PROMPT


def test_system_prompt_contains_terminal_conditions():
    assert TERMINAL_CONDITIONS in SYSTEM_PROMPT
    assert "check_terminal_conditions" in SYSTEM_PROMPT
    assert "mark_memo_generated" in SYSTEM_PROMPT
    assert "NEVER stop after the last planned query" in SYSTEM_PROMPT
    assert "PLANNED → EXECUTING → SUCCEEDED" in SYSTEM_PROMPT


def test_system_prompt_contains_model_quota_handling():
    assert MODEL_QUOTA_HANDLING in SYSTEM_PROMPT
    assert "record_model_error" in MODEL_QUOTA_HANDLING
    assert "check_quota_status" in MODEL_QUOTA_HANDLING
    assert "mark_analysis_incomplete" in MODEL_QUOTA_HANDLING
    assert "429" in MODEL_QUOTA_HANDLING
    assert "RESOURCE_EXHAUSTED" in MODEL_QUOTA_HANDLING
    assert "Do NOT re-execute" in MODEL_QUOTA_HANDLING


def test_system_prompt_contains_evidence_provenance():
    assert EVIDENCE_PROVENANCE in SYSTEM_PROMPT
    assert "query_id" in EVIDENCE_PROVENANCE
    assert "actual result" in EVIDENCE_PROVENANCE


def test_step_3_no_expected_output():
    """STEP 3 must not contain 'Expected Output:' — only purpose/objective."""
    assert "Expected Output:" not in STEP_3_PLAN
    assert "purpose" in STEP_3_PLAN.lower() or "Determine" in STEP_3_PLAN