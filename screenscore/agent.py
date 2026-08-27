import logging
import os
import sys

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from .prompt import (
    ORCHESTRATOR_PROMPT,
    SCHEMA_AGENT_PROMPT,
    QUERY_AGENT_PROMPT,
    EVIDENCE_AGENT_PROMPT,
    DECISION_AGENT_PROMPT,
)
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

# ---------------------------------------------------------------------------
# ClickHouse / MCP configuration
# ---------------------------------------------------------------------------

_ch_env: dict[str, str] = {
    "CLICKHOUSE_HOST": os.environ.get("CLICKHOUSE_HOST", "sql-clickhouse.clickhouse.com"),
    "CLICKHOUSE_PORT": os.environ.get("CLICKHOUSE_PORT", "8443"),
    "CLICKHOUSE_USER": os.environ.get("CLICKHOUSE_USER", "demo"),
    "CLICKHOUSE_PASSWORD": os.environ.get("CLICKHOUSE_PASSWORD", ""),
    "CLICKHOUSE_SECURE": os.environ.get("CLICKHOUSE_SECURE", "true"),
    "CLICKHOUSE_VERIFY": os.environ.get("CLICKHOUSE_VERIFY", "true"),
}

_mcp_python: str = sys.executable

# W4: Model name from env var so a typo doesn't crash the whole import
_MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite-preview-06-17")

_model = Gemini(
    model=_MODEL_NAME,
    retry_options=types.HttpRetryOptions(attempts=5, initial_delay=2.0, max_delay=30.0),
)

# W5: MCP toolset failure is logged loudly and gracefully handled
_mcp_toolset: McpToolset | None = None
_mcp_available = False
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
    _mcp_available = True
    logger.info("MCP ClickHouse toolset initialised (run_query, list_tables, list_databases)")
except Exception as e:
    logger.error(
        "STARTUP WARNING — MCP ClickHouse toolset FAILED to initialise: %s\n"
        "ClickHouse queries will not work. Check CLICKHOUSE_* environment variables.",
        e,
    )

# ---------------------------------------------------------------------------
# Sub-agent tool groups
# ---------------------------------------------------------------------------

_SCHEMA_TOOLS = [
    init_pipeline_state,
    get_schema_info,
    get_pipeline_status,
    update_step,
    update_research_status,
]

_QUERY_TOOLS = [
    plan_query,
    execute_query,
    retry_query,
    register_follow_up,
    diagnose_query_failure,
    plan_follow_up_queries,
    log_query_metadata,
    check_terminal_conditions,
    update_comparable_titles_status,
    record_model_error,
    check_quota_status,
    mark_analysis_incomplete,
    get_pipeline_status,
]

_EVIDENCE_TOOLS = [
    record_evidence,
    validate_claim,
    classify_candidate,
    get_evidence_status,
    get_audit_summary,
    format_table,
    generate_chart,
    get_title_performance,
]

_DECISION_TOOLS = [
    validate_analysis_constraints,
    mark_validation_complete,
    mark_decision_complete,
    generate_acquisition_memo,
    generate_html_memo,
    mark_memo_generated,
    check_terminal_conditions,
    get_pipeline_status,
]

# Attach MCP toolset to the query agent only
if _mcp_toolset is not None:
    _QUERY_TOOLS.append(_mcp_toolset)
else:
    logger.warning("MCP toolset unavailable — query_agent will not be able to execute ClickHouse SQL")

# ---------------------------------------------------------------------------
# Sub-agents
# ---------------------------------------------------------------------------

schema_agent = Agent(
    model=_model,
    name="schema_agent",
    description=(
        "Discovers the ClickHouse IMDb schema and initialises the pipeline state machine. "
        "Handles Steps 1–2 of the acquisition pipeline."
    ),
    instruction=SCHEMA_AGENT_PROMPT,
    tools=_SCHEMA_TOOLS,
)

query_agent = Agent(
    model=_model,
    name="query_agent",
    description=(
        "Plans and executes SQL queries against ClickHouse via MCP. "
        "Handles Steps 3–4 including adaptive recovery, retries, and follow-up queries."
    ),
    instruction=QUERY_AGENT_PROMPT,
    tools=_QUERY_TOOLS,
)

evidence_agent = Agent(
    model=_model,
    name="evidence_agent",
    description=(
        "Analyses query results, tracks evidence provenance, classifies candidates, "
        "and retrieves synthetic market benchmarks. Handles Steps 5–6."
    ),
    instruction=EVIDENCE_AGENT_PROMPT,
    tools=_EVIDENCE_TOOLS,
)

decision_agent = Agent(
    model=_model,
    name="decision_agent",
    description=(
        "Validates constraints, produces the ACQUIRE/PASS/FURTHER_REVIEW recommendation, "
        "and generates the structured acquisition memo and HTML report. Handles Steps 7–8."
    ),
    instruction=DECISION_AGENT_PROMPT,
    tools=_DECISION_TOOLS,
)

# ---------------------------------------------------------------------------
# Root orchestrator — delegates to sub-agents via transfer_to_agent
# ---------------------------------------------------------------------------

_root_agent = Agent(
    model=_model,
    name="screenscore",
    description=(
        "Studio Acquisition Analyst — evaluates film and TV titles for acquisition "
        "using IMDb data from ClickHouse, produces data-backed acquisition memos."
    ),
    instruction=ORCHESTRATOR_PROMPT,
    sub_agents=[schema_agent, query_agent, evidence_agent, decision_agent],
)

root_agent = _root_agent