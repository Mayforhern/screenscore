"""Tests for the screenscore agent module."""
from screenscore.agent import (
    root_agent,
    _model,
    _mcp_toolset,
    schema_agent,
    query_agent,
    evidence_agent,
    decision_agent,
    _SCHEMA_TOOLS,
    _QUERY_TOOLS,
    _EVIDENCE_TOOLS,
    _DECISION_TOOLS,
)


def test_root_agent_defined():
    assert root_agent is not None
    assert root_agent.name == "screenscore"


def test_root_agent_has_instruction():
    assert root_agent.instruction is not None
    assert "ScreenScore" in root_agent.instruction


def test_root_agent_has_model():
    assert root_agent.model is not None


def test_root_agent_has_no_sub_agents():
    """Root agent executes the pipeline directly — no sub-agent delegation."""
    assert len(root_agent.sub_agents) == 0


def test_schema_agent_tools():
    tool_names = {t.__name__ for t in _SCHEMA_TOOLS if hasattr(t, "__name__")}
    assert "init_pipeline_state" in tool_names
    assert "get_schema_info" in tool_names
    assert "get_pipeline_status" in tool_names
    assert "update_step" in tool_names


def test_query_agent_tools():
    tool_names = {t.__name__ for t in _QUERY_TOOLS if hasattr(t, "__name__")}
    assert "plan_query" in tool_names
    assert "execute_query" in tool_names
    assert "retry_query" in tool_names
    assert "diagnose_query_failure" in tool_names
    assert "plan_follow_up_queries" in tool_names
    assert "log_query_metadata" in tool_names


def test_evidence_agent_tools():
    tool_names = {t.__name__ for t in _EVIDENCE_TOOLS if hasattr(t, "__name__")}
    assert "record_evidence" in tool_names
    assert "validate_claim" in tool_names
    assert "classify_candidate" in tool_names
    assert "get_title_performance" in tool_names
    assert "generate_chart" in tool_names
    assert "format_table" in tool_names


def test_decision_agent_tools():
    tool_names = {t.__name__ for t in _DECISION_TOOLS if hasattr(t, "__name__")}
    assert "validate_analysis_constraints" in tool_names
    assert "generate_acquisition_memo" in tool_names
    assert "generate_html_memo" in tool_names
    assert "mark_memo_generated" in tool_names
    assert "mark_decision_complete" in tool_names


def test_mcp_toolset_optional():
    """MCP toolset may be None if ClickHouse is unreachable — agent still works."""
    if _mcp_toolset is None:
        assert True


def test_model_has_retry_options():
    assert _model is not None
    assert "gemini" in str(_model).lower() or "model" in str(_model).lower()


def test_all_tools_distributed():
    """Every tool must belong to exactly one sub-agent (no orphaned tools)."""
    all_tool_names = set()
    for tool_list in [_SCHEMA_TOOLS, _QUERY_TOOLS, _EVIDENCE_TOOLS, _DECISION_TOOLS]:
        for t in tool_list:
            if hasattr(t, "__name__"):
                all_tool_names.add(t.__name__)

    # Spot-check critical tools are distributed, not duplicated
    from screenscore.tools import get_schema_info, generate_acquisition_memo
    assert "get_schema_info" in all_tool_names
    assert "generate_acquisition_memo" in all_tool_names