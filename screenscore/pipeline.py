"""Pipeline state management for ScreenScore.

This module provides the state machine that enforces query lifecycle transitions,
terminal condition checking, maximum execution guards, and diagnostic logging.

State lives in ToolContext.state["pipeline"] as a dict with the following shape:

{
    "initialized": true,
    "max_queries": 20,
    "max_retries_per_query": 2,
    "max_follow_up_rounds": 3,
    "max_model_retries": 3,
    "current_step": "STEP 1 — SCHEMA",
    "completed_steps": [],
    "planned_queries": [],
    "executed_queries": [],
    "failed_queries": [],
    "retried_queries": {},
    "follow_up_queries": [],
    "pending_queries": [],
    "query_lifecycles": {},
    "research_status": "in_progress",
    "comparable_titles_status": "unresolved",
    "constraint_validation_status": "pending",
    "final_decision_status": "pending",
    "memo_generated": false,
    "validation_passed": false,
    "model_status": "ok",
    "model_errors": [],
    "quota_errors": [],
    "model_retry_count": 0,
    "last_model_error": null,
    "model_retry_after_seconds": null,
    "audit_trail": []
}
"""

import logging
import re
from typing import Any

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pipeline state keys
# ---------------------------------------------------------------------------
PIPELINE_STATE_KEY = "pipeline"

# Query lifecycle statuses
QUERY_PLANNED = "PLANNED"
QUERY_EXECUTING = "EXECUTING"
QUERY_SUCCEEDED = "SUCCEEDED"
QUERY_FAILED = "FAILED"
QUERY_DIAGNOSING = "DIAGNOSING"
QUERY_RETRYING = "RETRYING"
QUERY_UNRECOVERABLE = "UNRECOVERABLE"

# Research statuses
RESEARCH_IN_PROGRESS = "in_progress"
RESEARCH_COMPLETE = "complete"
RESEARCH_INSUFFICIENT = "insufficient_evidence"
RESEARCH_MAX_LIMIT = "max_research_limit_reached"

# Model statuses
MODEL_STATUS_OK = "ok"
MODEL_STATUS_DEGRADED = "degraded"
MODEL_STATUS_QUOTA_EXHAUSTED = "quota_exhausted"
MODEL_STATUS_UNAVAILABLE = "unavailable"

# Default limits
DEFAULT_MAX_QUERIES = 20
DEFAULT_MAX_RETRIES_PER_QUERY = 2
DEFAULT_MAX_FOLLOW_UP_ROUNDS = 3
DEFAULT_MAX_MODEL_RETRIES = 10


def _get_pipeline(state: dict) -> dict[str, Any]:
    """Get the pipeline state dict from ToolContext.state, creating if needed."""
    if PIPELINE_STATE_KEY not in state:
        state[PIPELINE_STATE_KEY] = _default_pipeline_state()
    return state[PIPELINE_STATE_KEY]


def _default_pipeline_state() -> dict[str, Any]:
    """Return a fresh pipeline state dict."""
    return {
        "initialized": False,
        "max_queries": DEFAULT_MAX_QUERIES,
        "max_retries_per_query": DEFAULT_MAX_RETRIES_PER_QUERY,
        "max_follow_up_rounds": DEFAULT_MAX_FOLLOW_UP_ROUNDS,
        "max_model_retries": DEFAULT_MAX_MODEL_RETRIES,
        "current_step": "",
        "completed_steps": [],
        "planned_queries": [],
        "executed_queries": [],
        "failed_queries": [],
        "retried_queries": {},
        "follow_up_queries": [],
        "pending_queries": [],
        "query_lifecycles": {},
        "research_status": RESEARCH_IN_PROGRESS,
        "comparable_titles_status": "unresolved",
        "constraint_validation_status": "pending",
        "final_decision_status": "pending",
        "memo_generated": False,
        "validation_passed": False,
        "model_status": MODEL_STATUS_OK,
        "model_errors": [],
        "quota_errors": [],
        "model_retry_count": 0,
        "last_model_error": None,
        "model_retry_after_seconds": 65.0,
        "audit_trail": [],
        "evidence_items": {},
        "evidence_claims": {},
        "evidence_dependencies": {},
        "candidate_classifications": {},
        "initially_planned_queries": [],
        "dynamic_follow_up_queries": [],
        "total_queries_executed": 0,
    }


def _log_transition(pipeline: dict, entry: str) -> None:
    """Append a timestamped transition log entry."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    full = f"[{ts}] {entry}"
    pipeline["audit_trail"].append(full)
    logger.info(entry)


def classify_model_error(error_message: str) -> dict[str, Any]:
    """Classify an error message as quota, transient, or other model error.

    Returns dict with:
      - is_model_error: bool
      - is_quota_error: bool
      - retry_after_seconds: float or None
      - error_type: str
    """
    msg_lower = error_message.lower()

    # Detect429 / RESOURCE_EXHAUSTED
    is_quota = (
        "429" in error_message
        or "resource_exhausted" in msg_lower
        or "resource exhausted" in msg_lower
        or "rate limit" in msg_lower
        or "ratelimit" in msg_lower
        or "too many requests" in msg_lower
    )

    # Detect transient errors (5xx, timeout, etc.)
    is_transient = (
        "500" in error_message
        or "502" in error_message
        or "503" in error_message
        or "504" in error_message
        or "timeout" in msg_lower
        or "deadline exceeded" in msg_lower
        or "unavailable" in msg_lower
    )

    is_model_error = is_quota or is_transient or any(
        keyword in msg_lower
        for keyword in [
            "generativelanguage",
            "gemini",
            "generate_content",
            "model",
            "token",
            "prompt",
        ]
    )

    # Parse RetryAfter / retry delay
    retry_after = None
    # Pattern: "retry in 28.8 seconds"
    retry_match = re.search(
        r"retry\s+(?:in|after)\s+([\d.]+)\s*(?:seconds?|s)", msg_lower
    )
    if retry_match:
        try:
            retry_after = float(retry_match.group(1))
        except ValueError:
            pass
    # Pattern: "RetryInfo: 28.8"
    if retry_after is None:
        retry_info_match = re.search(r"retry.?info:?\s*([\d.]+)", msg_lower)
        if retry_info_match:
            try:
                retry_after = float(retry_info_match.group(1))
            except ValueError:
                pass
    # Pattern: "Please retry in 28.8 seconds"
    if retry_after is None:
        please_retry = re.search(
            r"please\s+retry\s+in\s+([\d.]+)", msg_lower
        )
        if please_retry:
            try:
                retry_after = float(please_retry.group(1))
            except ValueError:
                pass

    if is_quota:
        error_type = "quota_exhausted"
    elif is_transient:
        error_type = "transient"
    elif is_model_error:
        error_type = "model_error"
    else:
        error_type = "unknown"

    return {
        "is_model_error": is_model_error,
        "is_quota_error": is_quota,
        "retry_after_seconds": retry_after,
        "error_type": error_type,
    }


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

async def init_pipeline_state(
    max_queries: int = DEFAULT_MAX_QUERIES,
    max_retries_per_query: int = DEFAULT_MAX_RETRIES_PER_QUERY,
    max_follow_up_rounds: int = DEFAULT_MAX_FOLLOW_UP_ROUNDS,
    max_model_retries: int = DEFAULT_MAX_MODEL_RETRIES,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Initialize pipeline state for a new analysis session.

    Must be called once at the start of the pipeline (STEP 1).
    Subsequent calls are no-ops if already initialized.
    """
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    state = tool_context.state
    pipeline = _get_pipeline(state)

    if pipeline.get("initialized"):
        _log_transition(pipeline, "[PIPELINE] Already initialized — no-op")
        return {
            "status": "success",
            "message": "Pipeline already initialized.",
            "max_queries": pipeline["max_queries"],
        }

    pipeline["initialized"] = True
    pipeline["max_queries"] = max_queries
    pipeline["max_retries_per_query"] = max_retries_per_query
    pipeline["max_follow_up_rounds"] = max_follow_up_rounds
    pipeline["max_model_retries"] = max_model_retries

    _log_transition(
        pipeline,
        f"[PIPELINE] INITIALIZED max_queries={max_queries} "
        f"max_retries={max_retries_per_query} max_follow_ups={max_follow_up_rounds} "
        f"max_model_retries={max_model_retries}",
    )

    return {
        "status": "success",
        "message": "Pipeline initialized.",
        "max_queries": max_queries,
        "max_retries_per_query": max_retries_per_query,
        "max_follow_up_rounds": max_follow_up_rounds,
        "max_model_retries": max_model_retries,
    }


# ---------------------------------------------------------------------------
# Query lifecycle
# ---------------------------------------------------------------------------

async def plan_query(
    query_id: str,
    purpose: str,
    sql_template: str,
    is_follow_up: bool | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Register a planned query in pipeline state.

    Call this BEFORE executing the query via run_query.
    Set is_follow_up=True explicitly for dynamic follow-up queries.
    If not set, the flag is inferred from the purpose string (legacy behaviour).
    """
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)

    # Check max queries limit
    total = len(pipeline["planned_queries"])
    if total >= pipeline["max_queries"]:
        _log_transition(
            pipeline,
            f"[PIPELINE] MAX QUERIES REACHED ({pipeline['max_queries']}) — "
            f"cannot plan {query_id}",
        )
        pipeline["research_status"] = RESEARCH_MAX_LIMIT
        return {
            "status": "blocked",
            "message": f"Maximum query limit ({pipeline['max_queries']}) reached. "
                       f"Cannot plan new queries. Proceed to validation.",
            "research_status": RESEARCH_MAX_LIMIT,
        }

    # Check if already planned
    if query_id in pipeline["planned_queries"]:
        return {
            "status": "noop",
            "message": f"Query {query_id} already planned.",
        }

    pipeline["planned_queries"].append(query_id)
    pipeline["pending_queries"].append(query_id)
    pipeline["query_lifecycles"][query_id] = {
        "status": QUERY_PLANNED,
        "purpose": purpose,
        "sql_template": sql_template,
        "attempt": 0,
        "rows_returned": None,
        "error": None,
        "recovery_action": None,
    }

    # Track query origin (initial vs follow-up)
    # Prefer explicit param; fall back to purpose-string heuristic for backwards compatibility
    _is_follow_up = is_follow_up if is_follow_up is not None else ("Follow-up to" in purpose)
    if _is_follow_up:
        pipeline.setdefault("dynamic_follow_up_queries", []).append(query_id)
    else:
        pipeline.setdefault("initially_planned_queries", []).append(query_id)

    _log_transition(
        pipeline,
        f"[QUERY] {query_id} PLANNED — {purpose}",
    )

    return {
        "status": "success",
        "query_id": query_id,
        "purpose": purpose,
        "planned_count": len(pipeline["planned_queries"]),
        "remaining_capacity": pipeline["max_queries"] - len(pipeline["planned_queries"]),
    }


async def execute_query(
    query_id: str,
    rows_returned: int,
    sql: str = "",
    error: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Record the result of a query execution.

    Call this AFTER each run_query call. Tracks the lifecycle:
    PLANNED -> EXECUTING -> SUCCEEDED / FAILED
    """
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)
    lc = pipeline["query_lifecycles"].get(query_id)

    if lc is None:
        # Auto-register if not planned (graceful handling)
        pipeline["planned_queries"].append(query_id)
        pipeline["query_lifecycles"][query_id] = {
            "status": QUERY_PLANNED,
            "purpose": "unplanned query",
            "sql_template": sql,
            "attempt": 0,
            "rows_returned": None,
            "error": None,
            "recovery_action": None,
        }
        lc = pipeline["query_lifecycles"][query_id]

    # Track total queries executed
    pipeline["total_queries_executed"] = pipeline.get("total_queries_executed", 0) + 1

    lc["attempt"] += 1
    lc["sql_template"] = sql or lc.get("sql_template", "")

    if error:
        lc["status"] = QUERY_FAILED
        lc["error"] = error
        lc["rows_returned"] = 0

        if query_id not in pipeline["failed_queries"]:
            pipeline["failed_queries"].append(query_id)

        if query_id in pipeline["pending_queries"]:
            pipeline["pending_queries"].remove(query_id)

        retry_count = pipeline["retried_queries"].get(query_id, 0)
        _log_transition(
            pipeline,
            f"[QUERY] {query_id} FAILED (attempt {lc['attempt']}) "
            f"error={error[:100]} retries={retry_count}",
        )

        return {
            "status": "failed",
            "query_id": query_id,
            "attempt": lc["attempt"],
            "error": error,
            "can_retry": retry_count < pipeline["max_retries_per_query"],
            "retry_count": retry_count,
        }
    else:
        lc["status"] = QUERY_SUCCEEDED
        lc["rows_returned"] = rows_returned
        lc["error"] = None

        if query_id not in pipeline["executed_queries"]:
            pipeline["executed_queries"].append(query_id)

        if query_id in pipeline["pending_queries"]:
            pipeline["pending_queries"].remove(query_id)

        if query_id in pipeline["failed_queries"]:
            pipeline["failed_queries"].remove(query_id)

        _log_transition(
            pipeline,
            f"[QUERY] {query_id} SUCCEEDED rows={rows_returned} "
            f"(attempt {lc['attempt']})",
        )

        return {
            "status": "success",
            "query_id": query_id,
            "rows_returned": rows_returned,
            "attempt": lc["attempt"],
        }


async def retry_query(
    query_id: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Register a retry attempt for a failed query.

    Call this BEFORE re-executing the query.
    Checks max retry limit and blocks if exceeded.
    """
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)
    lc = pipeline["query_lifecycles"].get(query_id)

    if lc is None:
        return {"status": "error", "message": f"Query {query_id} not found in pipeline state."}

    retry_count = pipeline["retried_queries"].get(query_id, 0)

    if retry_count >= pipeline["max_retries_per_query"]:
        lc["status"] = QUERY_UNRECOVERABLE
        _log_transition(
            pipeline,
            f"[QUERY] {query_id} UNRECOVERABLE — max retries ({pipeline['max_retries_per_query']}) exceeded",
        )
        return {
            "status": "blocked",
            "query_id": query_id,
            "message": f"Max retries ({pipeline['max_retries_per_query']}) exceeded for {query_id}. "
                       f"Marking as UNRECOVERABLE.",
            "retry_count": retry_count,
        }

    pipeline["retried_queries"][query_id] = retry_count + 1
    lc["status"] = QUERY_RETRYING
    lc["error"] = None

    _log_transition(
        pipeline,
        f"[QUERY] {query_id} RETRYING (attempt {lc['attempt'] + 1}, retry #{retry_count + 1})",
    )

    return {
        "status": "success",
        "query_id": query_id,
        "retry_count": retry_count + 1,
        "max_retries": pipeline["max_retries_per_query"],
    }


async def register_follow_up(
    query_id: str,
    parent_query_id: str,
    purpose: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Register a follow-up query spawned from an existing query."""
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)

    if query_id not in pipeline["follow_up_queries"]:
        pipeline["follow_up_queries"].append(query_id)

    return await plan_query(
        query_id=query_id,
        purpose=f"Follow-up to {parent_query_id}: {purpose}",
        sql_template="",
        tool_context=tool_context,
    )


# ---------------------------------------------------------------------------
# Terminal conditions
# ---------------------------------------------------------------------------

async def check_terminal_conditions(
    comparable_titles_count: int = 0,
    comparable_titles_required: int = 5,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Check whether all terminal conditions are satisfied for pipeline completion.

    The pipeline should only terminate when ALL conditions are met:
    - required queries completed or explicitly unresolvable
    - comparable_titles_status established
    - constraint audit completed
    - final acquisition decision established

    Returns:
        dict with 'all_met' bool, individual condition statuses, and
        'should_continue' bool.
    """
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)
    conditions = {}

    # 1. Are required queries completed?
    planned = set(pipeline["planned_queries"])
    executed = set(pipeline["executed_queries"])
    unrecoverable = {
        qid for qid, lc in pipeline["query_lifecycles"].items()
        if lc.get("status") == QUERY_UNRECOVERABLE
    }
    pending = set(pipeline["pending_queries"])

    # A query is "resolved" if it succeeded, was diagnosed, or is unrecoverable
    resolved = executed | unrecoverable
    # Unresolved = planned queries that haven't reached a terminal state
    # Note: queries still in pending_queries may have failed but not yet exhausted retries
    unresolved = planned - resolved

    conditions["queries_resolved"] = {
        "met": len(unresolved) == 0,
        "planned": len(planned),
        "executed": len(executed),
        "unrecoverable": len(unrecoverable),
        "pending": len(pending),
    }

    # 2. Is comparable_titles_status established?
    comp_status = pipeline.get("comparable_titles_status", "unresolved")
    conditions["comparable_titles_status"] = {
        "met": comp_status != "unresolved",
        "status": comp_status,
    }

    # 3. Has constraint validation completed?
    validation_status = pipeline.get("constraint_validation_status", "pending")
    conditions["constraint_validation"] = {
        "met": validation_status != "pending",
        "status": validation_status,
    }

    # 4. Has a final decision been made?
    decision_status = pipeline.get("final_decision_status", "pending")
    conditions["final_decision"] = {
        "met": decision_status != "pending",
        "status": decision_status,
    }

    # 5. Has the memo been generated?
    memo_generated = pipeline.get("memo_generated", False)
    conditions["memo_generated"] = {
        "met": memo_generated,
    }

    # 6. Max execution guard
    at_limit = len(pipeline["planned_queries"]) >= pipeline["max_queries"]
    conditions["max_queries"] = {
        "met": True,  # Always met — max_queries is an informational guard, not a blocking condition
        "planned": len(pipeline["planned_queries"]),
        "max": pipeline["max_queries"],
        "at_limit": at_limit,
    }

    # 7. Model availability — if quota exhausted and retries exhausted, analysis is incomplete
    model_status = pipeline.get("model_status", MODEL_STATUS_OK)
    model_retry_count = pipeline.get("model_retry_count", 0)
    max_model_retries = pipeline.get("max_model_retries", DEFAULT_MAX_MODEL_RETRIES)
    model_fully_exhausted = (
        model_status == MODEL_STATUS_QUOTA_EXHAUSTED
        and model_retry_count >= max_model_retries
    )
    conditions["model_available"] = {
        "met": not model_fully_exhausted,
        "model_status": model_status,
        "model_retry_count": model_retry_count,
        "max_model_retries": max_model_retries,
    }

    all_met = all(c["met"] for c in conditions.values())
    should_continue = not all_met

    _log_transition(
        pipeline,
        f"[PIPELINE] TERMINAL CHECK — all_met={all_met} "
        f"queries={conditions['queries_resolved']['met']} "
        f"comp_status={conditions['comparable_titles_status']['met']} "
        f"validation={conditions['constraint_validation']['met']} "
        f"decision={conditions['final_decision']['met']} "
        f"memo={conditions['memo_generated']['met']} "
        f"model={conditions['model_available']['met']}",
    )

    return {
        "all_met": all_met,
        "should_continue": should_continue,
        "conditions": conditions,
        "research_status": pipeline["research_status"],
    }


# ---------------------------------------------------------------------------
# Step transitions
# ---------------------------------------------------------------------------

async def update_step(
    step: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Update the current pipeline step and log the transition."""
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)

    old_step = pipeline.get("current_step", "")
    if old_step and old_step not in pipeline["completed_steps"]:
        pipeline["completed_steps"].append(old_step)

    pipeline["current_step"] = step

    _log_transition(pipeline, f"[PIPELINE] STEP started: {step}")

    return {
        "status": "success",
        "step": step,
        "completed_steps": pipeline["completed_steps"],
    }


async def update_research_status(
    status: str,
    reason: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Update the research status (e.g. when evidence is insufficient)."""
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)
    old_status = pipeline["research_status"]
    pipeline["research_status"] = status

    msg = f"[RESEARCH] Status: {old_status} -> {status}"
    if reason:
        msg += f" ({reason})"
    _log_transition(pipeline, msg)

    return {
        "status": "success",
        "research_status": status,
        "previous_status": old_status,
    }


async def update_comparable_titles_status(
    status: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Update the comparable titles status."""
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)
    old = pipeline["comparable_titles_status"]
    pipeline["comparable_titles_status"] = status

    _log_transition(pipeline, f"[RESEARCH] Comparable titles status: {old} -> {status}")

    return {
        "status": "success",
        "comparable_titles_status": status,
        "previous_status": old,
    }


async def mark_validation_complete(
    passed: bool,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Mark constraint validation as complete."""
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)
    pipeline["constraint_validation_status"] = "passed" if passed else "failed"
    pipeline["validation_passed"] = passed

    _log_transition(
        pipeline,
        f"[CONSTRAINT] Validation {'PASSED' if passed else 'FAILED'}",
    )

    return {
        "status": "success",
        "validation_passed": passed,
    }


async def mark_decision_complete(
    recommendation: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Mark the final decision as complete."""
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)
    pipeline["final_decision_status"] = "decided"

    _log_transition(
        pipeline,
        f"[PIPELINE] DECISION: {recommendation}",
    )

    return {
        "status": "success",
        "recommendation": recommendation,
    }


async def mark_memo_generated(
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Mark memo generation as complete."""
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)
    pipeline["memo_generated"] = True

    _log_transition(pipeline, "[PIPELINE] STEP 8 memo generation COMPLETE")

    return {"status": "success"}


# ---------------------------------------------------------------------------
# Model error handling
# ---------------------------------------------------------------------------

async def record_model_error(
    error_message: str,
    step: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Record a model (Gemini) error in pipeline state.

    Classifies the error as quota, transient, or other model error.
    If quota error, increments model_retry_count and sets retry delay.
    If max retries exceeded, transitions to QUOTA_EXHAUSTED.

    Call this when the agent detects a model error (e.g. from error output
    or when a tool call returns an error indicating model unavailability).
    """
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)
    classification = classify_model_error(error_message)

    if not classification["is_model_error"]:
        return {
            "status": "not_model_error",
            "message": "Error does not appear to be a model error.",
            "classification": classification,
        }

    # Record the error
    error_entry = {
        "message": error_message[:500],
        "step": step or pipeline.get("current_step", ""),
        "classification": classification["error_type"],
        "retry_after_seconds": classification["retry_after_seconds"],
    }
    pipeline["model_errors"].append(error_entry)
    pipeline["last_model_error"] = error_entry

    if classification["is_quota_error"]:
        pipeline["quota_errors"].append(error_entry)
        pipeline["model_retry_count"] += 1
        pipeline["model_status"] = MODEL_STATUS_DEGRADED

        if classification["retry_after_seconds"]:
            pipeline["model_retry_after_seconds"] = classification["retry_after_seconds"]

        retry_count = pipeline["model_retry_count"]
        max_retries = pipeline.get("max_model_retries", DEFAULT_MAX_MODEL_RETRIES)

        if retry_count >= max_retries:
            pipeline["model_status"] = MODEL_STATUS_QUOTA_EXHAUSTED
            _log_transition(
                pipeline,
                f"[MODEL] QUOTA_EXHAUSTED — max model retries ({max_retries}) reached. "
                f"Analysis may be incomplete.",
            )
            return {
                "status": "quota_exhausted",
                "model_status": MODEL_STATUS_QUOTA_EXHAUSTED,
                "model_retry_count": retry_count,
                "max_model_retries": max_retries,
                "retry_after_seconds": classification["retry_after_seconds"],
                "message": f"Model quota exhausted after {retry_count} retries. "
                           f"Proceeding with available evidence. Analysis may be incomplete.",
            }
        else:
            _log_transition(
                pipeline,
                f"[MODEL] QUOTA_ERROR (retry {retry_count}/{max_retries}) — "
                f"retry after {classification['retry_after_seconds']}s"
                if classification["retry_after_seconds"]
                else f"[MODEL] QUOTA_ERROR (retry {retry_count}/{max_retries})",
            )
            return {
                "status": "recorded",
                "model_status": MODEL_STATUS_DEGRADED,
                "model_retry_count": retry_count,
                "max_model_retries": max_retries,
                "retry_after_seconds": classification["retry_after_seconds"],
                "message": f"Quota error recorded. Retry {retry_count}/{max_retries}. "
                           f"Wait {classification['retry_after_seconds']}s before retrying."
                           if classification["retry_after_seconds"]
                           else f"Quota error recorded. Retry {retry_count}/{max_retries}.",
            }
    else:
        # Transient or other model error
        pipeline["model_status"] = MODEL_STATUS_DEGRADED
        _log_transition(
            pipeline,
            f"[MODEL] ERROR ({classification['error_type']}) — {error_message[:80]}",
        )
        return {
            "status": "recorded",
            "model_status": MODEL_STATUS_DEGRADED,
            "error_type": classification["error_type"],
            "message": f"Model error recorded: {classification['error_type']}",
        }


async def check_quota_status(
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Check current model quota status and return actionable information.

    Returns:
        - model_status: ok / degraded / quota_exhausted / unavailable
        - model_retry_count / max_model_retries
        - retry_after_seconds (if known)
        - can_retry: whether another retry is allowed
        - recommendation: what the agent should do
    """
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)
    model_status = pipeline.get("model_status", MODEL_STATUS_OK)
    retry_count = pipeline.get("model_retry_count", 0)
    max_retries = pipeline.get("max_model_retries", DEFAULT_MAX_MODEL_RETRIES)
    retry_after = pipeline.get("model_retry_after_seconds")
    can_retry = retry_count < max_retries

    if model_status == MODEL_STATUS_QUOTA_EXHAUSTED:
        recommendation = (
            "Proceed to constraint validation and memo generation with available evidence. "
            "Do NOT attempt more queries. Set final status to ANALYSIS_INCOMPLETE."
        )
    elif model_status == MODEL_STATUS_DEGRADED and can_retry:
        if retry_after:
            recommendation = (
                f"Wait {retry_after:.0f} seconds, then retry from current step. "
                f"Do NOT re-execute already successful queries."
            )
        else:
            recommendation = (
                "Retry from current step after a brief delay. "
                "Do NOT re-execute already successful queries."
            )
    elif model_status == MODEL_STATUS_DEGRADED and not can_retry:
        recommendation = (
            "Max model retries reached. Proceed to validation and memo with available evidence."
        )
    else:
        recommendation = "Model is available. Continue pipeline normally."

    return {
        "status": "success",
        "model_status": model_status,
        "model_retry_count": retry_count,
        "max_model_retries": max_retries,
        "retry_after_seconds": retry_after,
        "can_retry": can_retry,
        "recommendation": recommendation,
        "quota_error_count": len(pipeline.get("quota_errors", [])),
        "total_model_error_count": len(pipeline.get("model_errors", [])),
    }


async def mark_analysis_incomplete(
    reason: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Mark the pipeline as ANALYSIS_INCOMPLETE due to model quota exhaustion.

    This sets a controlled terminal state that prevents fabrication of
    recommendations from incomplete evidence.
    """
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)

    pipeline["research_status"] = RESEARCH_INSUFFICIENT
    pipeline["final_decision_status"] = "incomplete"

    # If comparable_titles_status is still unresolved, set it
    if pipeline.get("comparable_titles_status") == "unresolved":
        pipeline["comparable_titles_status"] = "unknown_incomplete"

    # If validation hasn't run, mark as skipped
    if pipeline.get("constraint_validation_status") == "pending":
        pipeline["constraint_validation_status"] = "skipped_incomplete"

    _log_transition(
        pipeline,
        f"[PIPELINE] ANALYSIS_INCOMPLETE — {reason or 'Model quota exhausted'}",
    )

    return {
        "status": "success",
        "analysis_status": "INCOMPLETE",
        "reason": reason or "Model quota exhausted before investigation could complete",
        "message": (
            "Pipeline marked as ANALYSIS_INCOMPLETE. "
            "Do NOT produce a confident ACQUIRE/PASS recommendation. "
            "FURTHER_REVIEW is the only appropriate recommendation if a memo is generated."
        ),
    }


# ---------------------------------------------------------------------------
# Status retrieval
# ---------------------------------------------------------------------------

async def get_pipeline_status(
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Retrieve the full pipeline status for diagnostic purposes."""
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    pipeline = _get_pipeline(tool_context.state)

    # Build summary
    query_summaries = {}
    for qid, lc in pipeline["query_lifecycles"].items():
        query_summaries[qid] = {
            "status": lc["status"],
            "purpose": lc.get("purpose", ""),
            "attempt": lc.get("attempt", 0),
            "rows_returned": lc.get("rows_returned"),
            "error": lc.get("error"),
        }

    # Query audit
    initially_planned = pipeline.get("initially_planned_queries", [])
    dynamic_follow_up = pipeline.get("dynamic_follow_up_queries", [])
    total_executed = pipeline.get("total_queries_executed", len(pipeline["executed_queries"]))

    # Evidence audit
    from screenscore.evidence import EvidenceRegistry
    evidence_data = pipeline.get("evidence_items", {})
    evidence_registry = EvidenceRegistry.from_dict({
        "items": evidence_data,
        "claims": pipeline.get("evidence_claims", {}),
        "dependencies": pipeline.get("evidence_dependencies", {}),
        "candidate_classifications": pipeline.get("candidate_classifications", {}),
    })
    audit_summary = evidence_registry.get_audit_summary()

    return {
        "status": "success",
        "pipeline": {
            "initialized": pipeline["initialized"],
            "current_step": pipeline["current_step"],
            "completed_steps": pipeline["completed_steps"],
            "planned_queries": pipeline["planned_queries"],
            "executed_queries": pipeline["executed_queries"],
            "failed_queries": pipeline["failed_queries"],
            "pending_queries": pipeline["pending_queries"],
            "follow_up_queries": pipeline["follow_up_queries"],
            "retry_counts": pipeline["retried_queries"],
            "research_status": pipeline["research_status"],
            "comparable_titles_status": pipeline["comparable_titles_status"],
            "constraint_validation_status": pipeline["constraint_validation_status"],
            "final_decision_status": pipeline["final_decision_status"],
            "memo_generated": pipeline["memo_generated"],
            "validation_passed": pipeline["validation_passed"],
            "max_queries": pipeline["max_queries"],
            "max_retries_per_query": pipeline["max_retries_per_query"],
            "max_follow_up_rounds": pipeline["max_follow_up_rounds"],
            "query_count": len(pipeline["planned_queries"]),
            "model_status": pipeline.get("model_status", MODEL_STATUS_OK),
            "model_retry_count": pipeline.get("model_retry_count", 0),
            "max_model_retries": pipeline.get("max_model_retries", DEFAULT_MAX_MODEL_RETRIES),
            "model_error_count": len(pipeline.get("model_errors", [])),
            "quota_error_count": len(pipeline.get("quota_errors", [])),
            "initially_planned_queries": initially_planned,
            "dynamic_follow_up_queries": dynamic_follow_up,
            "total_queries_executed": total_executed,
            "evidence_audit": audit_summary,
        },
        "query_lifecycles": query_summaries,
        "audit_trail": pipeline["audit_trail"],
    }


# ---------------------------------------------------------------------------
# Evidence tools
# ---------------------------------------------------------------------------

async def record_evidence(
    key: str,
    description: str,
    status: str,
    source_query: str = "",
    source_tool: str = "",
    value: Any = None,
    metadata: dict[str, Any] | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Record a piece of evidence collected during research.

    Status must be one of: verified, derived, not_verified, not_computable, contradicted.
    """
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    from screenscore.evidence import EvidenceRegistry, EvidenceStatus

    pipeline = _get_pipeline(tool_context.state)
    registry = EvidenceRegistry.from_dict({
        "items": pipeline.get("evidence_items", {}),
        "claims": pipeline.get("evidence_claims", {}),
        "dependencies": pipeline.get("evidence_dependencies", {}),
        "candidate_classifications": pipeline.get("candidate_classifications", {}),
    })

    try:
        # Accept both string and EvidenceStatus enum objects
        status_str = status.value if hasattr(status, "value") else str(status)
        evidence_status = EvidenceStatus(status_str.lower())
    except (ValueError, AttributeError):
        valid_statuses = [e.value for e in EvidenceStatus]
        return {
            "status": "error",
            "message": f"Invalid status '{status}'. Must be one of: {valid_statuses}",
        }


    item = registry.record_evidence(
        key=key,
        description=description,
        status=evidence_status,
        source_query=source_query or None,
        source_tool=source_tool or None,
        value=value,
        metadata=metadata,
    )

    # Persist back to pipeline state
    pipeline["evidence_items"] = registry.to_dict()["items"]
    pipeline["evidence_dependencies"] = registry.to_dict()["dependencies"]

    _log_transition(
        pipeline,
        f"[EVIDENCE] Recorded: {key} = {status} (source: {source_tool or 'unknown'})",
    )

    return {
        "status": "success",
        "evidence_key": key,
        "evidence_status": status,
        "description": description,
    }


async def validate_claim(
    claim_id: str,
    description: str,
    required_evidence: list[str],
    metadata: dict[str, Any] | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Validate an analytical claim against collected evidence.

    If all required evidence is verified/derived, claim status is 'supported'.
    Otherwise, claim status is 'gated' with list of missing evidence keys.
    """
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    from screenscore.evidence import EvidenceRegistry

    pipeline = _get_pipeline(tool_context.state)
    registry = EvidenceRegistry.from_dict({
        "items": pipeline.get("evidence_items", {}),
        "claims": pipeline.get("evidence_claims", {}),
        "dependencies": pipeline.get("evidence_dependencies", {}),
        "candidate_classifications": pipeline.get("candidate_classifications", {}),
    })

    claim = registry.record_claim(
        claim_id=claim_id,
        description=description,
        required_evidence=required_evidence,
        metadata=metadata,
    )

    # Persist back to pipeline state
    pipeline["evidence_claims"] = registry.to_dict()["claims"]

    claim_status_str = getattr(claim.status, "value", str(claim.status))
    _log_transition(
        pipeline,
        f"[EVIDENCE] Claim {claim_id}: {claim_status_str} "
        f"(gated_by: {claim.gated_by or 'none'})",
    )

    return {
        "status": "success",
        "claim_id": claim_id,
        "claim_status": claim_status_str,
        "gated_by": claim.gated_by,
        "required_evidence": required_evidence,
    }


async def classify_candidate(
    candidate_id: str,
    has_genre_overlap: bool,
    has_entity_match: bool,
    target_genres_verified: bool,
    metadata: dict[str, Any] | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Classify a candidate title based on evidence prerequisites.

    Classification depends on whether target genres are verified AND whether
    the candidate has genre overlap or entity match.
    """
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    from screenscore.evidence import EvidenceRegistry

    pipeline = _get_pipeline(tool_context.state)
    registry = EvidenceRegistry.from_dict({
        "items": pipeline.get("evidence_items", {}),
        "claims": pipeline.get("evidence_claims", {}),
        "dependencies": pipeline.get("evidence_dependencies", {}),
        "candidate_classifications": pipeline.get("candidate_classifications", {}),
    })

    classification = registry.classify_candidate(
        candidate_id=candidate_id,
        has_genre_overlap=has_genre_overlap,
        has_entity_match=has_entity_match,
        target_genres_verified=target_genres_verified,
        metadata=metadata,
    )

    # Persist back to pipeline state
    pipeline["candidate_classifications"] = registry.to_dict()["candidate_classifications"]

    classification_str = getattr(classification, "value", str(classification))
    _log_transition(
        pipeline,
        f"[EVIDENCE] Candidate {candidate_id} classified as {classification_str}",
    )

    return {
        "status": "success",
        "candidate_id": candidate_id,
        "classification": classification_str,
        "target_genres_verified": target_genres_verified,
        "has_genre_overlap": has_genre_overlap,
        "has_entity_match": has_entity_match,
    }


async def get_evidence_status(
    key: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Get the status of a specific evidence item."""
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    from screenscore.evidence import EvidenceRegistry

    pipeline = _get_pipeline(tool_context.state)
    registry = EvidenceRegistry.from_dict({
        "items": pipeline.get("evidence_items", {}),
        "claims": pipeline.get("evidence_claims", {}),
        "dependencies": pipeline.get("evidence_dependencies", {}),
        "candidate_classifications": pipeline.get("candidate_classifications", {}),
    })

    evidence_status = registry.get_evidence_status(key)

    if evidence_status is None:
        return {
            "status": "not_found",
            "message": f"Evidence key '{key}' not found.",
        }

    return {
        "status": "success",
        "evidence_key": key,
        "evidence_status": getattr(evidence_status, "value", str(evidence_status)),
    }


async def get_audit_summary(
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Get a summary of all evidence, claims, and candidate classifications for audit."""
    if not tool_context:
        return {"status": "error", "message": "tool_context required"}

    from screenscore.evidence import EvidenceRegistry

    pipeline = _get_pipeline(tool_context.state)
    registry = EvidenceRegistry.from_dict({
        "items": pipeline.get("evidence_items", {}),
        "claims": pipeline.get("evidence_claims", {}),
        "dependencies": pipeline.get("evidence_dependencies", {}),
        "candidate_classifications": pipeline.get("candidate_classifications", {}),
    })

    audit_summary = registry.get_audit_summary()

    return {
        "status": "success",
        "audit_summary": audit_summary,
    }
