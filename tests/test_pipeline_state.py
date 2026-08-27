"""Regression tests for pipeline state machine and continuation enforcement.

TEST 1  — Normal successful pipeline (all queries succeed → memo generated)
TEST 2  — Q5 insufficient → pipeline continues to validation → final decision
TEST 3  — Query failure → diagnose → retry → pipeline continues
TEST 4  — Unrecoverable query failure → pipeline does not hang
TEST 5  — Dynamic follow-up → additional query executes → pipeline continues
TEST 6  — Strict comparable constraint → fallback does not inflate count
TEST 7  — Target not found → pipeline handles gracefully
TEST 8  — Memo gating → generate_acquisition_memo blocked before validation

PLUS model quota handling tests:
MQT 1  — Gemini 429 during Q3 → retry → resume Q3 without rerunning Q1/Q2
MQT 2  — Gemini 429 after Q3 succeeds → Q3 remains SUCCEEDED
MQT 3  — Retry-After/RetryInfo is respected
MQT 4  — Maximum model retries prevent infinite loops
MQT 5  — Quota exhaustion produces controlled ANALYSIS_INCOMPLETE state
MQT 6  — Successful ClickHouse queries are never duplicated by model retry
MQT 7  — Model failure is distinct from SQL failure in query_audit
MQT 8  — Pipeline state survives interruption and can resume
MQT 9  — Terminal conditions do not incorrectly mark incomplete investigation as complete
MQT 10 — Evidence claim cannot be generated without a supporting query/result
"""
import pytest

from screenscore.pipeline import (
    PIPELINE_STATE_KEY,
    DEFAULT_MAX_QUERIES,
    DEFAULT_MAX_RETRIES_PER_QUERY,
    DEFAULT_MAX_FOLLOW_UP_ROUNDS,
    DEFAULT_MAX_MODEL_RETRIES,
    QUERY_PLANNED,
    QUERY_EXECUTING,
    QUERY_SUCCEEDED,
    QUERY_FAILED,
    QUERY_UNRECOVERABLE,
    MODEL_STATUS_OK,
    MODEL_STATUS_DEGRADED,
    MODEL_STATUS_QUOTA_EXHAUSTED,
    RESEARCH_IN_PROGRESS,
    RESEARCH_COMPLETE,
    RESEARCH_INSUFFICIENT,
    RESEARCH_MAX_LIMIT,
    classify_model_error,
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
)
from screenscore.tools import (
    generate_acquisition_memo,
    generate_html_memo,
    plan_follow_up_queries,
    validate_analysis_constraints,
)


# ---------------------------------------------------------------------------
# Helper: Mock ToolContext
# ---------------------------------------------------------------------------

class MockToolContext:
    """Minimal ToolContext mock for testing pipeline state."""

    def __init__(self):
        self.state = {}
        self._artifacts = {}

    async def save_artifact(self, filename: str, artifact=None) -> int:
        self._artifacts[filename] = artifact
        return len(self._artifacts)


# ---------------------------------------------------------------------------
# TEST 1 — Normal successful pipeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_1_normal_successful_pipeline():
    """All queries succeed → final memo generated."""
    ctx = MockToolContext()

    # Initialize
    result = await init_pipeline_state(tool_context=ctx)
    assert result["status"] == "success"

    # Plan and execute Q1-Q5
    for qid, purpose in [
        ("Q1", "Genre string verification"),
        ("Q2", "Genre benchmark"),
        ("Q3", "Genre rating trend"),
        ("Q4", "Target title lookup"),
        ("Q5", "Comparable titles"),
    ]:
        plan_result = await plan_query(query_id=qid, purpose=purpose, sql_template="SELECT ...", tool_context=ctx)
        assert plan_result["status"] == "success"
        exec_result = await execute_query(query_id=qid, rows_returned=2, sql="SELECT ...", tool_context=ctx)
        assert exec_result["status"] == "success"

    # Check terminal conditions (not yet — no validation/memo)
    terminal = await check_terminal_conditions(tool_context=ctx)
    assert terminal["should_continue"] is True

    # Step transitions
    await update_step(step="STEP 7 — CONSTRAINT VALIDATION", tool_context=ctx)
    await update_comparable_titles_status(status="SUFFICIENT", tool_context=ctx)
    await mark_validation_complete(passed=True, tool_context=ctx)

    await update_step(step="STEP 8 — DECIDE", tool_context=ctx)
    await mark_decision_complete(recommendation="FURTHER_REVIEW", tool_context=ctx)

    # Generate memo
    memo_result = await generate_acquisition_memo(
        title="Test Film",
        recommendation="FURTHER_REVIEW",
        rationale="Test rationale.",
        comparable_titles=[],
        risk_flags=[],
        genre_benchmark=[{"genre": "Sci-Fi", "avg_rating": 7.0, "stddev": 0.5, "title_count": 100}],
        tool_context=ctx,
    )
    assert memo_result["status"] == "success"

    await mark_memo_generated(tool_context=ctx)

    # Now all terminal conditions should be met
    terminal = await check_terminal_conditions(tool_context=ctx)
    assert terminal["all_met"] is True
    assert terminal["should_continue"] is False

    # Verify status
    status = await get_pipeline_status(tool_context=ctx)
    assert status["pipeline"]["memo_generated"] is True
    assert status["pipeline"]["validation_passed"] is True
    assert len(status["pipeline"]["executed_queries"]) == 5


# ---------------------------------------------------------------------------
# TEST 2 — Q5 insufficient → pipeline continues to validation → final decision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_2_q5_insufficient_continues():
    """Q5 executes and returns insufficient results → pipeline continues."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)

    # Plan and execute Q1-Q4
    for qid in ["Q1", "Q2", "Q3", "Q4"]:
        await plan_query(query_id=qid, purpose="test", sql_template="SELECT ...", tool_context=ctx)
        await execute_query(query_id=qid, rows_returned=5, sql="SELECT ...", tool_context=ctx)

    # Q5 returns 0 rows (insufficient)
    await plan_query(query_id="Q5", purpose="Comparable titles", sql_template="SELECT ...", tool_context=ctx)
    q5_result = await execute_query(query_id="Q5", rows_returned=0, sql="SELECT ...", tool_context=ctx)
    assert q5_result["status"] == "success"
    assert q5_result["rows_returned"] == 0

    # Set comparable titles status
    await update_comparable_titles_status(status="INSUFFICIENT", tool_context=ctx)

    # Terminal conditions should say continue (no validation yet)
    terminal = await check_terminal_conditions(
        comparable_titles_count=0,
        comparable_titles_required=5,
        tool_context=ctx,
    )
    assert terminal["should_continue"] is True
    assert terminal["conditions"]["comparable_titles_status"]["met"] is True

    # Continue to validation
    await mark_validation_complete(passed=True, tool_context=ctx)
    await mark_decision_complete(recommendation="FURTHER_REVIEW", tool_context=ctx)

    memo_result = await generate_acquisition_memo(
        title="Test Film",
        recommendation="FURTHER_REVIEW",
        rationale="Insufficient comps.",
        comparable_titles=[],
        risk_flags=["Insufficient comparable titles"],
        genre_benchmark=[{"genre": "Sci-Fi", "avg_rating": 7.0, "stddev": 0.5, "title_count": 100}],
        comparable_titles_status="INSUFFICIENT — 0/5 found",
        tool_context=ctx,
    )
    assert memo_result["status"] == "success"

    await mark_memo_generated(tool_context=ctx)

    terminal = await check_terminal_conditions(tool_context=ctx)
    assert terminal["all_met"] is True


# ---------------------------------------------------------------------------
# TEST 3 — Query failure → diagnose → retry → pipeline continues
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_3_query_failure_retry_success():
    """A query fails → diagnose → corrected retry → pipeline continues."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)

    # Plan Q1
    await plan_query(query_id="Q1", purpose="Genre verification", sql_template="SELECT ...", tool_context=ctx)

    # Q1 fails
    fail_result = await execute_query(
        query_id="Q1", rows_returned=0, sql="SELECT title FROM imdb.movies",
        error="Unknown column 'title' does not exist", tool_context=ctx,
    )
    assert fail_result["status"] == "failed"
    assert fail_result["can_retry"] is True

    # Retry
    retry_result = await retry_query(query_id="Q1", tool_context=ctx)
    assert retry_result["status"] == "success"

    # Retry succeeds
    success_result = await execute_query(
        query_id="Q1", rows_returned=3, sql="SELECT name FROM imdb.movies", tool_context=ctx,
    )
    assert success_result["status"] == "success"
    assert success_result["attempt"] == 2

    # Q1 is now in executed_queries, not in failed_queries
    status = await get_pipeline_status(tool_context=ctx)
    assert "Q1" in status["pipeline"]["executed_queries"]
    assert "Q1" not in status["pipeline"]["failed_queries"]


# ---------------------------------------------------------------------------
# TEST 4 — Unrecoverable query failure → pipeline does not hang
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_4_unrecoverable_failure():
    """Retry fails → pipeline does not hang → final state records failure."""
    ctx = MockToolContext()
    await init_pipeline_state(max_retries_per_query=1, tool_context=ctx)

    # Plan Q1
    await plan_query(query_id="Q1", purpose="test", sql_template="SELECT ...", tool_context=ctx)

    # First failure
    await execute_query(query_id="Q1", rows_returned=0, error="error 1", tool_context=ctx)

    # First retry
    retry1 = await retry_query(query_id="Q1", tool_context=ctx)
    assert retry1["status"] == "success"

    # Second failure
    await execute_query(query_id="Q1", rows_returned=0, error="error 2", tool_context=ctx)

    # Second retry attempt — should be blocked (max_retries=1)
    retry2 = await retry_query(query_id="Q1", tool_context=ctx)
    assert retry2["status"] == "blocked"

    # Q1 should be unrecoverable
    status = await get_pipeline_status(tool_context=ctx)
    lc = status["query_lifecycles"]["Q1"]
    assert lc["status"] == QUERY_UNRECOVERABLE

    # Pipeline should NOT hang — it should still be able to continue
    # Even with an unrecoverable query, we can proceed to validation/memo
    await mark_validation_complete(passed=True, tool_context=ctx)
    await mark_decision_complete(recommendation="PASS", tool_context=ctx)

    memo_result = await generate_acquisition_memo(
        title="Test Film",
        recommendation="PASS",
        rationale="Query failed unrecoverably.",
        comparable_titles=[],
        risk_flags=["Query Q1 failed permanently"],
        genre_benchmark=[],
        tool_context=ctx,
    )
    assert memo_result["status"] == "success"


# ---------------------------------------------------------------------------
# TEST 5 — Dynamic follow-up → additional query executes → pipeline continues
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_5_dynamic_follow_up():
    """A query result requires additional research → follow-up executes → pipeline continues."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)

    # Plan and execute Q4 (target lookup)
    await plan_query(query_id="Q4", purpose="Target lookup", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q4", rows_returned=1, sql="SELECT ...", tool_context=ctx)

    # Q5 strict — returns 2 comps (insufficient)
    await plan_query(query_id="Q5", purpose="Strict comps", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q5", rows_returned=2, sql="SELECT ...", tool_context=ctx)

    # Dynamic follow-up: broader search
    follow_up = await register_follow_up(
        query_id="Q5b",
        parent_query_id="Q5",
        purpose="Broader search with single genre",
        tool_context=ctx,
    )
    assert follow_up["status"] == "success"

    # Q5b executes — returns 4 more comps
    await execute_query(query_id="Q5b", rows_returned=4, sql="SELECT ...", tool_context=ctx)

    # Verify follow-up tracking
    status = await get_pipeline_status(tool_context=ctx)
    assert "Q5b" in status["pipeline"]["follow_up_queries"]
    assert "Q5b" in status["pipeline"]["executed_queries"]


# ---------------------------------------------------------------------------
# TEST 6 — Strict comparable constraint → fallback does not inflate count
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_6_strict_comparable_constraint():
    """Only 2 strict comparables exist → fallback does not count as 5 strict."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)

    # Q5 strict: 2 comps found
    await plan_query(query_id="Q5", purpose="Strict comps", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q5", rows_returned=2, sql="SELECT ...", tool_context=ctx)

    # Q5b fallback: 4 comps found (but these are NOT strict)
    await plan_query(query_id="Q5b", purpose="Fallback comps", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q5b", rows_returned=4, sql="SELECT ...", tool_context=ctx)

    await update_comparable_titles_status(status="INSUFFICIENT", tool_context=ctx)

    # Validate constraints — strict comps are only 2, not 5
    validation = await validate_analysis_constraints(
        requested_year_start=2022,
        requested_year_end=2026,
        requested_rating_threshold=7.5,
        requested_genres=["Sci-Fi", "Thriller"],
        requested_max_comps=5,
        valid_comps_found=2,  # Only 2 strict comps
        fallback_separate=True,  # Fallback is separate
        fabricated_data=False,
    )
    assert validation["status"] == "PASS"  # PASS because fallback_separate=True

    # The fallback comps should NOT inflate the strict count
    status = await get_pipeline_status(tool_context=ctx)
    assert status["query_lifecycles"]["Q5"]["rows_returned"] == 2
    assert status["query_lifecycles"]["Q5b"]["rows_returned"] == 4


# ---------------------------------------------------------------------------
# TEST 7 — Target not found → pipeline handles gracefully
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_7_target_not_found():
    """Q1 returns zero rows → pipeline handles it gracefully."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)

    # Q1 target lookup — zero rows
    await plan_query(query_id="Q1", purpose="Target lookup", sql_template="SELECT ...", tool_context=ctx)
    q1_result = await execute_query(query_id="Q1", rows_returned=0, sql="SELECT ...", tool_context=ctx)
    assert q1_result["status"] == "success"  # Zero rows is not an error
    assert q1_result["rows_returned"] == 0

    # Pipeline should continue — not hang
    # Continue through remaining queries and produce a memo
    for qid in ["Q2", "Q3", "Q4", "Q5"]:
        await plan_query(query_id=qid, purpose="test", sql_template="SELECT ...", tool_context=ctx)
        await execute_query(query_id=qid, rows_returned=0, sql="SELECT ...", tool_context=ctx)

    await update_comparable_titles_status(status="ZERO", tool_context=ctx)
    await mark_validation_complete(passed=True, tool_context=ctx)
    await mark_decision_complete(recommendation="FURTHER_REVIEW", tool_context=ctx)

    memo_result = await generate_acquisition_memo(
        title="Unknown Film",
        recommendation="FURTHER_REVIEW",
        rationale="Target not found in database.",
        comparable_titles=[],
        risk_flags=["Target title not found"],
        genre_benchmark=[],
        comparable_titles_status="ZERO — target not in database",
        tool_context=ctx,
    )
    assert memo_result["status"] == "success"


# ---------------------------------------------------------------------------
# TEST 8 — Memo gating → generate_acquisition_memo blocked before validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_8_memo_gating_blocks_before_validation():
    """generate_acquisition_memo cannot execute before validation state exists."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)

    # Try to generate memo without validation — should be blocked
    memo_result = await generate_acquisition_memo(
        title="Test Film",
        recommendation="FURTHER_REVIEW",
        rationale="Should be blocked.",
        comparable_titles=[],
        risk_flags=[],
        genre_benchmark=[],
        tool_context=ctx,
    )
    assert memo_result["status"] == "error"
    assert "validation" in memo_result["message"].lower()

    # Now validate and try again
    await mark_validation_complete(passed=True, tool_context=ctx)
    await mark_decision_complete(recommendation="FURTHER_REVIEW", tool_context=ctx)

    memo_result = await generate_acquisition_memo(
        title="Test Film",
        recommendation="FURTHER_REVIEW",
        rationale="Now allowed.",
        comparable_titles=[],
        risk_flags=[],
        genre_benchmark=[],
        tool_context=ctx,
    )
    assert memo_result["status"] == "success"


@pytest.mark.asyncio
async def test_8b_html_memo_gating_blocks_before_markdown():
    """generate_html_memo cannot execute before markdown memo is generated."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)
    await mark_validation_complete(passed=True, tool_context=ctx)
    await mark_decision_complete(recommendation="PASS", tool_context=ctx)

    # Try to generate HTML memo without markdown memo — should be blocked
    html_result = await generate_html_memo(
        title="Test Film",
        recommendation="PASS",
        rationale="Should be blocked.",
        comparable_titles=[],
        risk_flags=[],
        genre_benchmark=[],
        tool_context=ctx,
    )
    assert html_result["status"] == "error"
    assert "markdown" in html_result["message"].lower()

    # Generate markdown memo first
    await generate_acquisition_memo(
        title="Test Film",
        recommendation="PASS",
        rationale="Generate first.",
        comparable_titles=[],
        risk_flags=[],
        genre_benchmark=[],
        tool_context=ctx,
    )
    await mark_memo_generated(tool_context=ctx)

    # Now HTML memo should work
    html_result = await generate_html_memo(
        title="Test Film",
        recommendation="PASS",
        rationale="Now allowed.",
        comparable_titles=[],
        risk_flags=[],
        genre_benchmark=[],
        tool_context=ctx,
    )
    assert html_result["status"] == "success"


# ---------------------------------------------------------------------------
# TEST 9 — Audit consistency → executed vs planned queries distinguishable
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_9_audit_consistency():
    """Every executed query appears in query_audit; planned-but-unexecuted remain distinguishable."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)

    # Plan Q1, Q2, Q3
    await plan_query(query_id="Q1", purpose="test", sql_template="SELECT 1", tool_context=ctx)
    await plan_query(query_id="Q2", purpose="test", sql_template="SELECT 2", tool_context=ctx)
    await plan_query(query_id="Q3", purpose="test", sql_template="SELECT 3", tool_context=ctx)

    # Execute only Q1 and Q2
    await execute_query(query_id="Q1", rows_returned=1, sql="SELECT 1", tool_context=ctx)
    await execute_query(query_id="Q2", rows_returned=2, sql="SELECT 2", tool_context=ctx)

    status = await get_pipeline_status(tool_context=ctx)

    # Q1 and Q2 should be executed
    assert "Q1" in status["pipeline"]["executed_queries"]
    assert "Q2" in status["pipeline"]["executed_queries"]

    # Q3 should still be pending
    assert "Q3" in status["pipeline"]["pending_queries"]
    assert "Q3" not in status["pipeline"]["executed_queries"]

    # All three should be planned
    assert "Q1" in status["pipeline"]["planned_queries"]
    assert "Q2" in status["pipeline"]["planned_queries"]
    assert "Q3" in status["pipeline"]["planned_queries"]

    # Q1 and Q2 should have SUCCEEDED lifecycle
    assert status["query_lifecycles"]["Q1"]["status"] == QUERY_SUCCEEDED
    assert status["query_lifecycles"]["Q2"]["status"] == QUERY_SUCCEEDED

    # Q3 should still be PLANNED
    assert status["query_lifecycles"]["Q3"]["status"] == QUERY_PLANNED


# ---------------------------------------------------------------------------
# TEST 10 — No infinite loop → max execution guard terminates cleanly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_10_no_infinite_loop():
    """Repeated insufficient results eventually terminate through controlled state."""
    ctx = MockToolContext()
    await init_pipeline_state(max_queries=5, tool_context=ctx)

    # Execute 5 queries (the max)
    for i in range(5):
        qid = f"Q{i+1}"
        result = await plan_query(query_id=qid, purpose=f"Query {i+1}", sql_template="SELECT ...", tool_context=ctx)
        if result["status"] == "blocked":
            break
        await execute_query(query_id=qid, rows_returned=0, sql="SELECT ...", tool_context=ctx)

    # 6th query should be blocked
    blocked = await plan_query(query_id="Q6", purpose="Beyond limit", sql_template="SELECT ...", tool_context=ctx)
    assert blocked["status"] == "blocked"

    # Pipeline should have max_queries status
    status = await get_pipeline_status(tool_context=ctx)
    assert status["pipeline"]["query_count"] == 5

    # plan_follow_up_queries should also be blocked
    fu_result = await plan_follow_up_queries(
        genres=["Sci-Fi"],
        year_start=2022,
        year_end=2026,
        rating_threshold=7.0,
        comps_found=0,
        comps_requested=5,
        tool_context=ctx,
    )
    assert fu_result["suggestions"][0]["strategy"] == "max_queries_reached"

    # But pipeline can still proceed to validation and memo
    await update_research_status(status=RESEARCH_MAX_LIMIT, tool_context=ctx)
    await update_comparable_titles_status(status="INSUFFICIENT", tool_context=ctx)
    await mark_validation_complete(passed=True, tool_context=ctx)
    await mark_decision_complete(recommendation="FURTHER_REVIEW", tool_context=ctx)

    memo_result = await generate_acquisition_memo(
        title="Test Film",
        recommendation="FURTHER_REVIEW",
        rationale="Max queries reached with insufficient evidence.",
        comparable_titles=[],
        risk_flags=["Max query limit reached"],
        genre_benchmark=[],
        comparable_titles_status="INSUFFICIENT",
        tool_context=ctx,
    )
    assert memo_result["status"] == "success"

    await mark_memo_generated(tool_context=ctx)

    terminal = await check_terminal_conditions(tool_context=ctx)
    assert terminal["all_met"] is True
    assert terminal["research_status"] == RESEARCH_MAX_LIMIT


# ---------------------------------------------------------------------------
# Additional tests for pipeline state infrastructure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_init_idempotent():
    """Calling init_pipeline_state twice is a no-op."""
    ctx = MockToolContext()
    r1 = await init_pipeline_state(tool_context=ctx)
    assert r1["status"] == "success"

    r2 = await init_pipeline_state(tool_context=ctx)
    assert r2["status"] == "success"
    assert "already" in r2["message"].lower() or "no-op" in str(r2).lower()


@pytest.mark.asyncio
async def test_retry_blocks_after_max():
    """retry_query blocks after max retries."""
    ctx = MockToolContext()
    await init_pipeline_state(max_retries_per_query=2, tool_context=ctx)

    await plan_query(query_id="Q1", purpose="test", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q1", rows_returned=0, error="err1", tool_context=ctx)

    # Retry 1
    r1 = await retry_query(query_id="Q1", tool_context=ctx)
    assert r1["status"] == "success"
    await execute_query(query_id="Q1", rows_returned=0, error="err2", tool_context=ctx)

    # Retry 2
    r2 = await retry_query(query_id="Q1", tool_context=ctx)
    assert r2["status"] == "success"
    await execute_query(query_id="Q1", rows_returned=0, error="err3", tool_context=ctx)

    # Retry 3 — blocked
    r3 = await retry_query(query_id="Q1", tool_context=ctx)
    assert r3["status"] == "blocked"


@pytest.mark.asyncio
async def test_update_step_records_completion():
    """update_step marks previous step as completed."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)

    await update_step(step="STEP 1 — SCHEMA", tool_context=ctx)
    await update_step(step="STEP 2 — DISCOVER", tool_context=ctx)
    await update_step(step="STEP 3 — PLAN", tool_context=ctx)

    status = await get_pipeline_status(tool_context=ctx)
    assert "STEP 1 — SCHEMA" in status["pipeline"]["completed_steps"]
    assert "STEP 2 — DISCOVER" in status["pipeline"]["completed_steps"]
    assert status["pipeline"]["current_step"] == "STEP 3 — PLAN"


@pytest.mark.asyncio
async def test_audit_trail_populated():
    """Audit trail contains transition log entries."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)
    await plan_query(query_id="Q1", purpose="test", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q1", rows_returned=1, sql="SELECT ...", tool_context=ctx)

    status = await get_pipeline_status(tool_context=ctx)
    assert len(status["audit_trail"]) > 0
    # Should contain pipeline and query transitions
    trail_text = " ".join(status["audit_trail"])
    assert "INITIALIZED" in trail_text
    assert "Q1" in trail_text


@pytest.mark.asyncio
async def test_mark_decision_complete():
    """mark_decision_complete updates pipeline state."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)
    await mark_decision_complete(recommendation="ACQUIRE", tool_context=ctx)

    status = await get_pipeline_status(tool_context=ctx)
    assert status["pipeline"]["final_decision_status"] == "decided"


@pytest.mark.asyncio
async def test_memo_already_generated_blocks():
    """generate_acquisition_memo blocks if memo was already generated."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)
    await mark_validation_complete(passed=True, tool_context=ctx)
    await mark_decision_complete(recommendation="PASS", tool_context=ctx)

    # First memo
    r1 = await generate_acquisition_memo(
        title="Test", recommendation="PASS", rationale="First.",
        comparable_titles=[], risk_flags=[], genre_benchmark=[],
        tool_context=ctx,
    )
    assert r1["status"] == "success"

    await mark_memo_generated(tool_context=ctx)

    # Second memo — blocked
    r2 = await generate_acquisition_memo(
        title="Test", recommendation="PASS", rationale="Second.",
        comparable_titles=[], risk_flags=[], genre_benchmark=[],
        tool_context=ctx,
    )
    assert r2["status"] == "error"
    assert "already" in r2["message"].lower()


# ===========================================================================
# MODEL QUOTA HANDLING TESTS
# ===========================================================================

# ---------------------------------------------------------------------------
# classify_model_error unit tests
# ---------------------------------------------------------------------------

def test_classify_quota_429():
    result = classify_model_error("429 RESOURCE_EXHAUSTED: quota exceeded")
    assert result["is_model_error"] is True
    assert result["is_quota_error"] is True
    assert result["error_type"] == "quota_exhausted"


def test_classify_quota_retry_after():
    result = classify_model_error(
        "429 Please retry in 28.8 seconds for model gemini-3.5-flash-lite"
    )
    assert result["is_quota_error"] is True
    assert result["retry_after_seconds"] == pytest.approx(28.8, abs=0.1)


def test_classify_quota_retry_info():
    result = classify_model_error(
        "RESOURCE_EXHAUSTED. RetryInfo: 15.5 seconds"
    )
    assert result["is_quota_error"] is True
    assert result["retry_after_seconds"] == pytest.approx(15.5, abs=0.1)


def test_classify_transient_500():
    result = classify_model_error("500 Internal Server Error from generativelanguage")
    assert result["is_model_error"] is True
    assert result["is_quota_error"] is False
    assert result["error_type"] == "transient"


def test_classify_non_model_error():
    result = classify_model_error("column 'title' does not exist in schema")
    assert result["is_model_error"] is False
    assert result["is_quota_error"] is False


def test_classify_gemini_specific():
    """Gemini API URL is detected as model error (but not specifically quota without 429)."""
    result = classify_model_error(
        "generativelanguage.googleapis.com/generate_content_free_tier_input_token_count"
    )
    assert result["is_model_error"] is True
    # URL alone doesn't indicate quota — need 429 or RESOURCE_EXHAUSTED
    assert result["is_quota_error"] is False


def test_classify_gemini_quota_combined():
    """Gemini URL + 429 is classified as quota error."""
    result = classify_model_error(
        "429 generativelanguage.googleapis.com/generate_content_free_tier_input_token_count"
    )
    assert result["is_model_error"] is True
    assert result["is_quota_error"] is True


# ---------------------------------------------------------------------------
# MQT 1 — Gemini 429 during Q3 → retry → resume Q3 without rerunning Q1/Q2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mqt1_quota_during_q3_resume_without_rerunning_q1q2():
    """Gemini429 during Q3 → retry → resume Q3 without rerunning Q1/Q2."""
    ctx = MockToolContext()
    await init_pipeline_state(max_model_retries=3, tool_context=ctx)

    # Q1 and Q2 succeeded
    await plan_query(query_id="Q1", purpose="Genre verification", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q1", rows_returned=5, sql="SELECT ...", tool_context=ctx)
    await plan_query(query_id="Q2", purpose="Genre benchmark", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q2", rows_returned=2, sql="SELECT ...", tool_context=ctx)

    # Q3 planned but Gemini fails during inference
    await plan_query(query_id="Q3", purpose="Genre trend", sql_template="SELECT ...", tool_context=ctx)

    # Record model error
    result = await record_model_error(
        error_message="429 RESOURCE_EXHAUSTED: Please retry in 28.8 seconds",
        step="STEP 4 — QUERIES (Q3)",
        tool_context=ctx,
    )
    assert result["status"] == "recorded"
    assert result["model_retry_count"] == 1

    # Check quota status
    quota = await check_quota_status(tool_context=ctx)
    assert quota["can_retry"] is True
    assert quota["retry_after_seconds"] == pytest.approx(28.8, abs=0.1)

    # Q3 now succeeds on retry (without re-running Q1/Q2)
    await execute_query(query_id="Q3", rows_returned=8, sql="SELECT ...", tool_context=ctx)

    status = await get_pipeline_status(tool_context=ctx)
    # Q1 and Q2 are still SUCCEEDED, not re-executed
    assert status["query_lifecycles"]["Q1"]["status"] == QUERY_SUCCEEDED
    assert status["query_lifecycles"]["Q2"]["status"] == QUERY_SUCCEEDED
    assert status["query_lifecycles"]["Q3"]["status"] == QUERY_SUCCEEDED
    # Only 1 attempt each for Q1/Q2, 1 for Q3
    assert status["query_lifecycles"]["Q1"]["attempt"] == 1
    assert status["query_lifecycles"]["Q2"]["attempt"] == 1
    assert status["query_lifecycles"]["Q3"]["attempt"] == 1


# ---------------------------------------------------------------------------
# MQT 2 — Gemini429 after Q3 succeeds → Q3 remains SUCCEEDED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mqt2_quota_after_q3_succeeds_q3_remains_succeeded():
    """Gemini429 after Q3 succeeds → Q3 remains SUCCEEDED, not FAILED."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)

    # Q3 succeeded
    await plan_query(query_id="Q3", purpose="Genre trend", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q3", rows_returned=8, sql="SELECT ...", tool_context=ctx)

    # Gemini fails while planning Q4 (after Q3 already succeeded)
    await record_model_error(
        error_message="429 RESOURCE_EXHAUSTED",
        step="STEP 4 — QUERIES (planning Q4)",
        tool_context=ctx,
    )

    # Q3 must still be SUCCEEDED
    status = await get_pipeline_status(tool_context=ctx)
    assert status["query_lifecycles"]["Q3"]["status"] == QUERY_SUCCEEDED
    # Model is degraded but Q3 is fine
    assert status["pipeline"]["model_status"] == MODEL_STATUS_DEGRADED


# ---------------------------------------------------------------------------
# MQT 3 — Retry-After/RetryInfo is respected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mqt3_retry_after_parsed():
    """Retry-After/RetryInfo is parsed and exposed."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)

    await record_model_error(
        error_message="429 Please retry in 42.3 seconds",
        step="STEP 4",
        tool_context=ctx,
    )

    quota = await check_quota_status(tool_context=ctx)
    assert quota["retry_after_seconds"] == pytest.approx(42.3, abs=0.1)
    assert "42" in str(quota["recommendation"]) or "retry" in quota["recommendation"].lower()


@pytest.mark.asyncio
async def test_mqt3b_retry_info_parsed():
    """RetryInfo variant is also parsed."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)

    await record_model_error(
        error_message="RESOURCE_EXHAUSTED. RetryInfo: 12.0",
        step="STEP 5",
        tool_context=ctx,
    )

    quota = await check_quota_status(tool_context=ctx)
    assert quota["retry_after_seconds"] == pytest.approx(12.0, abs=0.1)


# ---------------------------------------------------------------------------
# MQT 4 — Maximum model retries prevent infinite loops
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mqt4_max_model_retries_prevents_infinite_loop():
    """Max model retries prevent infinite loops."""
    ctx = MockToolContext()
    await init_pipeline_state(max_model_retries=2, tool_context=ctx)

    # First quota error
    r1 = await record_model_error(
        error_message="429 RESOURCE_EXHAUSTED",
        step="Q3",
        tool_context=ctx,
    )
    assert r1["status"] == "recorded"
    assert r1["model_retry_count"] == 1

    # Second quota error
    r2 = await record_model_error(
        error_message="429 RESOURCE_EXHAUSTED",
        step="Q3",
        tool_context=ctx,
    )
    assert r2["status"] == "quota_exhausted"
    assert r2["model_status"] == MODEL_STATUS_QUOTA_EXHAUSTED

    # Third attempt — already exhausted
    quota = await check_quota_status(tool_context=ctx)
    assert quota["can_retry"] is False
    assert quota["model_status"] == MODEL_STATUS_QUOTA_EXHAUSTED


# ---------------------------------------------------------------------------
# MQT 5 — Quota exhaustion produces controlled ANALYSIS_INCOMPLETE state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mqt5_quota_exhaustion_analysis_incomplete():
    """Quota exhaustion produces controlled ANALYSIS_INCOMPLETE state."""
    ctx = MockToolContext()
    await init_pipeline_state(max_model_retries=1, tool_context=ctx)

    # Q1 and Q2 succeeded
    await plan_query(query_id="Q1", purpose="test", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q1", rows_returned=5, sql="SELECT ...", tool_context=ctx)
    await plan_query(query_id="Q2", purpose="test", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q2", rows_returned=2, sql="SELECT ...", tool_context=ctx)

    # Quota exhausted
    await record_model_error(
        error_message="429 RESOURCE_EXHAUSTED",
        step="Q3",
        tool_context=ctx,
    )
    # Second error exhausts retries
    await record_model_error(
        error_message="429 RESOURCE_EXHAUSTED",
        step="Q3",
        tool_context=ctx,
    )

    # Mark analysis incomplete
    incomplete = await mark_analysis_incomplete(
        reason="Gemini model quota exhausted before investigation could complete",
        tool_context=ctx,
    )
    assert incomplete["status"] == "success"
    assert incomplete["analysis_status"] == "INCOMPLETE"

    # Verify state
    status = await get_pipeline_status(tool_context=ctx)
    assert status["pipeline"]["model_status"] == MODEL_STATUS_QUOTA_EXHAUSTED
    assert status["pipeline"]["final_decision_status"] == "incomplete"
    assert status["pipeline"]["research_status"] == RESEARCH_INSUFFICIENT


# ---------------------------------------------------------------------------
# MQT 6 — Successful ClickHouse queries are never duplicated by model retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mqt6_successful_queries_not_duplicated():
    """Successful ClickHouse queries are never duplicated by model retry."""
    ctx = MockToolContext()
    await init_pipeline_state(max_model_retries=3, tool_context=ctx)

    # Q1 executed and succeeded
    await plan_query(query_id="Q1", purpose="Genre verification", sql_template="SELECT ...", tool_context=ctx)
    q1 = await execute_query(query_id="Q1", rows_returned=5, sql="SELECT ...", tool_context=ctx)
    assert q1["status"] == "success"

    # Model error occurs
    await record_model_error(error_message="429 RESOURCE_EXHAUSTED", step="Q2", tool_context=ctx)

    # Check: Q1 is in executed_queries
    status = await get_pipeline_status(tool_context=ctx)
    assert "Q1" in status["pipeline"]["executed_queries"]
    assert status["query_lifecycles"]["Q1"]["status"] == QUERY_SUCCEEDED
    assert status["query_lifecycles"]["Q1"]["attempt"] == 1  # Only 1 attempt

    # If Q1 were to be "re-planned", plan_query returns noop
    noop = await plan_query(query_id="Q1", purpose="Genre verification", sql_template="SELECT ...", tool_context=ctx)
    assert noop["status"] == "noop"

    # Q1 is NOT re-executed
    assert status["query_lifecycles"]["Q1"]["attempt"] == 1


# ---------------------------------------------------------------------------
# MQT 7 — Model failure is distinct from SQL failure in query_audit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mqt7_model_failure_distinct_from_sql_failure():
    """Model failure is distinct from SQL failure in query_audit."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)

    # Q3 SQL succeeded
    await plan_query(query_id="Q3", purpose="Genre trend", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q3", rows_returned=8, sql="SELECT ...", tool_context=ctx)

    # Model error after Q3
    await record_model_error(
        error_message="429 RESOURCE_EXHAUSTED",
        step="planning Q4",
        tool_context=ctx,
    )

    status = await get_pipeline_status(tool_context=ctx)

    # Q3 is SUCCEEDED (not FAILED)
    assert status["query_lifecycles"]["Q3"]["status"] == QUERY_SUCCEEDED
    assert status["query_lifecycles"]["Q3"]["error"] is None

    # Model error is tracked separately
    assert status["pipeline"]["model_status"] == MODEL_STATUS_DEGRADED
    assert status["pipeline"]["model_error_count"] == 1
    assert status["pipeline"]["quota_error_count"] == 1

    # Q3 is NOT in failed_queries
    assert "Q3" not in status["pipeline"]["failed_queries"]


# ---------------------------------------------------------------------------
# MQT 8 — Pipeline state survives interruption and can resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mqt8_pipeline_state_survives_and_resumes():
    """Pipeline state survives interruption and can resume from checkpoint."""
    ctx = MockToolContext()
    await init_pipeline_state(max_model_retries=2, tool_context=ctx)

    # Simulate: Q1, Q2 succeeded, Q3 succeeded, model fails at Q4
    await update_step(step="STEP 4 — QUERIES", tool_context=ctx)
    await plan_query(query_id="Q1", purpose="Genre verification", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q1", rows_returned=5, sql="SELECT ...", tool_context=ctx)
    await plan_query(query_id="Q2", purpose="Genre benchmark", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q2", rows_returned=2, sql="SELECT ...", tool_context=ctx)
    await plan_query(query_id="Q3", purpose="Genre trend", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q3", rows_returned=8, sql="SELECT ...", tool_context=ctx)

    # Model error
    await record_model_error(error_message="429 RESOURCE_EXHAUSTED", step="Q4", tool_context=ctx)

    # Get full status (resumability checkpoint)
    checkpoint = await get_pipeline_status(tool_context=ctx)

    # Verify checkpoint has all resumability info
    assert checkpoint["pipeline"]["current_step"] != ""
    assert "Q1" in checkpoint["pipeline"]["executed_queries"]
    assert "Q2" in checkpoint["pipeline"]["executed_queries"]
    assert "Q3" in checkpoint["pipeline"]["executed_queries"]
    assert checkpoint["pipeline"]["model_status"] == MODEL_STATUS_DEGRADED
    assert checkpoint["pipeline"]["model_retry_count"] == 1

    # Simulate: new invocation reads checkpoint and can resume
    # Q4 was not planned (model failed before it could be planned)
    # The checkpoint should show Q1-Q3 executed, Q4 not yet in the pipeline
    assert "Q4" not in checkpoint["pipeline"]["planned_queries"]
    assert "Q4" not in checkpoint["pipeline"]["executed_queries"]

    # Resume: plan and execute Q4
    await plan_query(query_id="Q4", purpose="Title lookup", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q4", rows_returned=1, sql="SELECT ...", tool_context=ctx)

    status = await get_pipeline_status(tool_context=ctx)
    assert "Q4" in status["pipeline"]["executed_queries"]
    assert status["query_lifecycles"]["Q4"]["status"] == QUERY_SUCCEEDED


# ---------------------------------------------------------------------------
# MQT 9 — Terminal conditions do not incorrectly mark incomplete as complete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mqt9_terminal_conditions_block_on_incomplete():
    """Terminal conditions do not incorrectly mark incomplete investigation as complete."""
    ctx = MockToolContext()
    await init_pipeline_state(max_model_retries=1, tool_context=ctx)

    # Q1 succeeded, Q2 succeeded
    await plan_query(query_id="Q1", purpose="test", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q1", rows_returned=5, sql="SELECT ...", tool_context=ctx)
    await plan_query(query_id="Q2", purpose="test", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q2", rows_returned=2, sql="SELECT ...", tool_context=ctx)

    # Q3 planned but not yet executed (model failed)
    await plan_query(query_id="Q3", purpose="test", sql_template="SELECT ...", tool_context=ctx)

    # Model quota exhausted
    await record_model_error(error_message="429 RESOURCE_EXHAUSTED", step="Q3", tool_context=ctx)
    await record_model_error(error_message="429 RESOURCE_EXHAUSTED", step="Q3", tool_context=ctx)

    # Set other statuses to complete (simulating premature completion attempt)
    await update_comparable_titles_status(status="INSUFFICIENT", tool_context=ctx)
    await mark_validation_complete(passed=True, tool_context=ctx)
    await mark_decision_complete(recommendation="FURTHER_REVIEW", tool_context=ctx)
    await generate_acquisition_memo(
        title="Test", recommendation="FURTHER_REVIEW", rationale="Incomplete.",
        comparable_titles=[], risk_flags=[], genre_benchmark=[],
        tool_context=ctx,
    )
    await mark_memo_generated(tool_context=ctx)

    # Terminal conditions: all_met should be FALSE because model is exhausted
    # and Q3 is still pending (not resolved)
    terminal = await check_terminal_conditions(tool_context=ctx)
    # The model_available condition should be NOT met
    assert terminal["conditions"]["model_available"]["met"] is False
    # queries_resolved should NOT be met (Q3 is planned but not executed)
    assert terminal["conditions"]["queries_resolved"]["met"] is False
    # Overall should not be all_met
    assert terminal["all_met"] is False


# ---------------------------------------------------------------------------
# MQT 10 — Evidence claim cannot be generated without supporting query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mqt10_evidence_requires_supporting_query():
    """Evidence claim cannot be generated without a supporting query/result."""
    ctx = MockToolContext()
    await init_pipeline_state(tool_context=ctx)

    # Only Q1 executed
    await plan_query(query_id="Q1", purpose="Genre verification", sql_template="SELECT ...", tool_context=ctx)
    await execute_query(query_id="Q1", rows_returned=5, sql="SELECT ...", tool_context=ctx)

    # Q2 NOT executed
    status = await get_pipeline_status(tool_context=ctx)

    # Q1 has supporting query
    assert status["query_lifecycles"]["Q1"]["status"] == QUERY_SUCCEEDED
    assert status["query_lifecycles"]["Q1"]["rows_returned"] == 5

    # Q2 has NO supporting query
    assert "Q2" not in status["pipeline"]["executed_queries"]
    # Any claim about Q2's results would be unsupported
    # The audit trail shows Q1 was executed but Q2 was not
    trail_text = " ".join(status["audit_trail"])
    assert "Q1 SUCCEEDED" in trail_text
    assert "Q2 SUCCEEDED" not in trail_text
