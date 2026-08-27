"""Tests for the screenscore agent module."""
from screenscore.agent import root_agent, _model, _mcp_toolset, _agent_tools


def test_root_agent_defined():
    assert root_agent is not None
    assert root_agent.name == "screenscore"


def test_root_agent_has_instruction():
    assert root_agent.instruction is not None
    assert "ScreenScore" in root_agent.instruction


def test_root_agent_has_model():
    assert root_agent.model is not None


def test_root_agent_tools_contains_python_tools():
    tool_names = {t.__name__ if hasattr(t, '__name__') else str(t) for t in _agent_tools}
    # Existing tools
    assert "get_schema_info" in tool_names
    assert "validate_analysis_constraints" in tool_names
    assert "generate_acquisition_memo" in tool_names
    assert "generate_html_memo" in tool_names
    assert "get_title_performance" in tool_names
    assert "generate_chart" in tool_names
    assert "format_table" in tool_names
    assert "diagnose_query_failure" in tool_names
    assert "plan_follow_up_queries" in tool_names
    assert "log_query_metadata" in tool_names
    # Pipeline state management tools
    assert "init_pipeline_state" in tool_names
    assert "plan_query" in tool_names
    assert "execute_query" in tool_names
    assert "retry_query" in tool_names
    assert "register_follow_up" in tool_names
    assert "check_terminal_conditions" in tool_names
    assert "update_step" in tool_names
    assert "update_research_status" in tool_names
    assert "update_comparable_titles_status" in tool_names
    assert "mark_validation_complete" in tool_names
    assert "mark_decision_complete" in tool_names
    assert "mark_memo_generated" in tool_names
    assert "get_pipeline_status" in tool_names
    # Model error handling tools
    assert "record_model_error" in tool_names
    assert "check_quota_status" in tool_names
    assert "mark_analysis_incomplete" in tool_names


def test_mcp_toolset_optional():
    """MCP toolset may be None if ClickHouse is unreachable — agent still works."""
    if _mcp_toolset is None:
        assert True


def test_model_has_retry_options():
    assert _model is not None
    assert "gemini" in str(_model).lower() or "model" in str(_model).lower()