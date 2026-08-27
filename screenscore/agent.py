import logging
import os
import sys

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from .prompt import SYSTEM_PROMPT
from .tools import (
    diagnose_query_failure,
    format_table,
    generate_acquisition_memo,
    generate_chart,
    generate_html_memo,
    get_schema_info,
    get_title_performance,
    log_query_metadata,
    plan_follow_up_queries,
    validate_analysis_constraints,
)
from .pipeline import (
    init_pipeline_state,
    plan_query,
    execute_query,
    retry_query,
    register_follow_up,
    check_terminal_conditions,
    update_step,
    update_research_status,
    update_comparable_titles_status,
    mark_validation_complete,
    mark_decision_complete,
    mark_memo_generated,
    get_pipeline_status,
    record_model_error,
    check_quota_status,
    mark_analysis_incomplete,
    record_evidence,
    validate_claim,
    classify_candidate,
    get_evidence_status,
    get_audit_summary,
)

logger = logging.getLogger(__name__)

_ch_env: dict[str, str] = {
    "CLICKHOUSE_HOST": os.environ.get("CLICKHOUSE_HOST", "sql-clickhouse.clickhouse.com"),
    "CLICKHOUSE_PORT": os.environ.get("CLICKHOUSE_PORT", "8443"),
    "CLICKHOUSE_USER": os.environ.get("CLICKHOUSE_USER", "demo"),
    "CLICKHOUSE_PASSWORD": os.environ.get("CLICKHOUSE_PASSWORD", ""),
    "CLICKHOUSE_SECURE": os.environ.get("CLICKHOUSE_SECURE", "true"),
    "CLICKHOUSE_VERIFY": os.environ.get("CLICKHOUSE_VERIFY", "true"),
}

_mcp_python: str = sys.executable

_model = Gemini(
    model="gemini-3.5-flash-lite",
    retry_options=types.HttpRetryOptions(attempts=5, initial_delay=2.0, max_delay=30.0),
)

_mcp_toolset: McpToolset | None = None
try:
    _mcp_toolset = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=_mcp_python,
                args=["-m", "mcp_clickhouse.main"],
                env=_ch_env,
            ),
            timeout=60,
        ),
        tool_filter=["run_query", "list_databases", "list_tables"],
    )
except Exception as e:
    logger.error("Failed to initialize MCP ClickHouse toolset: %s", e)

_agent_tools: list = [
    # Pipeline state management
    init_pipeline_state,
    plan_query,
    execute_query,
    retry_query,
    register_follow_up,
    check_terminal_conditions,
    update_step,
    update_research_status,
    update_comparable_titles_status,
    mark_validation_complete,
    mark_decision_complete,
    mark_memo_generated,
    get_pipeline_status,
    # Model error handling
    record_model_error,
    check_quota_status,
    mark_analysis_incomplete,
    # Evidence tracking
    record_evidence,
    validate_claim,
    classify_candidate,
    get_evidence_status,
    get_audit_summary,
    # Existing tools
    diagnose_query_failure,
    get_schema_info,
    validate_analysis_constraints,
    generate_acquisition_memo,
    generate_html_memo,
    get_title_performance,
    generate_chart,
    format_table,
    plan_follow_up_queries,
    log_query_metadata,
]

if _mcp_toolset is not None:
    _agent_tools.append(_mcp_toolset)
else:
    logger.warning("MCP toolset is unavailable — ClickHouse queries will not work")

_root_agent = Agent(
    model=_model,
    name="screenscore",
    description=(
        "Studio Acquisition Analyst — evaluates film and TV titles for acquisition "
        "using IMDb data from ClickHouse, produces data-backed acquisition memos."
    ),
    instruction=SYSTEM_PROMPT,
    tools=_agent_tools,
)

root_agent = _root_agent