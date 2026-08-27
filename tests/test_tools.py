"""Tests for screenscore tools."""
import pytest

from screenscore.tools import (
    diagnose_query_failure,
    generate_html_memo,
    get_schema_info,
    get_title_performance,
    log_query_metadata,
    plan_follow_up_queries,
    validate_analysis_constraints,
    format_table,
    generate_chart,
    generate_acquisition_memo,
)


@pytest.mark.asyncio
async def test_get_schema_info():
    result = await get_schema_info()
    assert result["status"] == "verified"
    assert "tables" in result
    assert "imdb.movies" in result["tables"]
    assert "dataset_coverage" in result
    assert result["dataset_coverage"]["max_year"] == 2008
    assert "unavailable_fields" in result
    assert "vote_count" in result["unavailable_fields"]


@pytest.mark.asyncio
async def test_get_title_performance_found():
    result = await get_title_performance("Oppenheimer")
    assert result["status"] == "found"
    assert result["title"] == "oppenheimer"
    assert result["source_label"] == "[Synthetic Benchmark]"
    assert "opening_week_usd_m" in result


@pytest.mark.asyncio
async def test_get_title_performance_not_found():
    result = await get_title_performance("nonexistent_movie_xyz")
    assert result["status"] == "not_found"
    assert "available_titles" in result


@pytest.mark.asyncio
async def test_get_title_performance_ambiguous():
    result = await get_title_performance("the")
    assert result["status"] == "ambiguous"


@pytest.mark.asyncio
async def test_validate_analysis_constraints_pass():
    result = await validate_analysis_constraints(
        requested_year_start=2022,
        requested_year_end=2026,
        requested_rating_threshold=7.5,
        requested_genres=["Sci-Fi", "Thriller"],
        requested_max_comps=5,
        valid_comps_found=0,
        fallback_separate=True,
        fabricated_data=False,
    )
    assert result["status"] == "PASS"
    assert result["proceed_to_memo"] is True


@pytest.mark.asyncio
async def test_validate_analysis_constraints_fail_fabricated():
    result = await validate_analysis_constraints(
        requested_year_start=2022,
        requested_year_end=2026,
        requested_rating_threshold=7.5,
        requested_genres=["Sci-Fi"],
        requested_max_comps=5,
        valid_comps_found=0,
        fallback_separate=False,
        fabricated_data=True,
    )
    assert result["status"] == "FAIL"
    assert result["proceed_to_memo"] is False
    assert "fallback_separate" in result["failures"]
    assert "no_fabricated_data" in result["failures"]


@pytest.mark.asyncio
async def test_format_table():
    result = await format_table(
        headers=["Title", "Rating", "Year"],
        rows=[["Inception", "8.8", "2010"], ["Interstellar", "8.6", "2014"]],
        title="Top Movies",
    )
    assert result["status"] == "success"
    assert "markdown" in result
    assert "| Title | Rating | Year |" in result["markdown"]
    assert "| Inception | 8.8 | 2010 |" in result["markdown"]
    assert "### Top Movies" in result["markdown"]


@pytest.mark.asyncio
async def test_generate_chart():
    result = await generate_chart(
        chart_type="bar",
        title="Genre Ratings",
        x_label="Genre",
        y_label="Avg Rating",
        data_points=[{"label": "Action", "value": 7.2}, {"label": "Drama", "value": 6.8}],
    )
    assert result["status"] == "success"
    assert "chart_config" in result


@pytest.mark.asyncio
async def test_generate_chart_with_context():
    result = await generate_chart(
        chart_type="pie",
        title="Market Share",
        x_label="Platform",
        y_label="Share",
        data_points=[{"label": "Netflix", "value": 45}, {"label": "Disney+", "value": 30}],
    )
    assert result["status"] == "success"
    assert "chart_config" in result


@pytest.mark.asyncio
async def test_generate_acquisition_memo_basic():
    result = await generate_acquisition_memo(
        title="Test Film",
        recommendation="FURTHER_REVIEW",
        rationale="The genre average of 7.2 [ClickHouse IMDb] provides a reference benchmark. "
                   "No director was supplied and no modern comps exist (dataset ends 2008), "
                   "warranting further review before commitment.",
        comparable_titles=[],
        risk_flags=["No modern comparable titles exist — dataset ends 2008",
                     "No director supplied for track record analysis"],
        genre_benchmark={"genre": "Sci-Fi", "avg_rating": 7.2, "stddev": 0.5, "title_count": 120},
        comparable_titles_status="Zero titles matched: genre=Sci-Fi+Thriller, year=2022–2026.",
        constraint_violations=["Year range 2022–2026 is outside dataset coverage (1888–2008)"],
        sql_queries_run=["SELECT * FROM imdb.genres LIMIT 1"],
        sql_plan="1. Q1 — Genre probe\n2. Q2 — Genre benchmark",
        title_metadata=[
            {"field": "Name", "value": "Test Film", "source": "[User-provided]"},
        ],
        director_analysis=[
            {"field": "Director", "value": "Not supplied", "source": "[User-provided — omitted]"},
        ],
        constraint_audit={
            "year_range_preserved": "YES",
            "rating_threshold_preserved": "YES",
            "both_genres_required": "YES",
            "max_comps_respected": "YES",
            "historical_fallback_separated": "YES",
            "no_fabricated_data": "YES",
            "synthetic_data_labeled": "YES",
            "post_2015_trend_supported": "NO",
        },
    )
    assert result["status"] == "success"
    assert result["recommendation"] == "FURTHER_REVIEW"
    assert "memo_markdown" in result
    assert "memo_json" in result


@pytest.mark.asyncio
async def test_generate_acquisition_memo_invalid_recommendation():
    result = await generate_acquisition_memo(
        title="Test",
        recommendation="INVALID",
        rationale="Some rationale.",
        comparable_titles=[],
        risk_flags=[],
        genre_benchmark={},
    )
    assert result["status"] == "error"
    assert "recommendation must be one of" in result["message"]


@pytest.mark.asyncio
async def test_generate_acquisition_memo_acquire():
    result = await generate_acquisition_memo(
        title="Test Film",
        recommendation="ACQUIRE",
        rationale="Strong genre average of 7.8 [ClickHouse IMDb] supports acquisition."
                   " Comparable titles show consistent performance across the segment.",
        comparable_titles=[{"title": "Comp A", "year": 2005, "rank": 8.0}],
        risk_flags=[],
        genre_benchmark=[{"genre": "Sci-Fi", "avg_rating": 7.2, "stddev": 0.5, "title_count": 120}],
    )
    assert result["status"] == "success"
    assert result["recommendation"] == "ACQUIRE"


@pytest.mark.asyncio
async def test_plan_follow_up_queries_outside_dataset():
    """When year range is entirely outside dataset, suggests historical fallback."""
    result = await plan_follow_up_queries(
        genres=["Sci-Fi", "Thriller"],
        year_start=2022,
        year_end=2026,
        rating_threshold=7.5,
        comps_found=0,
        comps_requested=5,
    )
    assert result["assessment"]["needs_broadening"] is True
    assert len(result["suggestions"]) > 0
    suggestion = result["suggestions"][0]
    assert suggestion["strategy"] == "use_historical_fallback"
    assert "2008" in str(suggestion.get("sql_template", ""))


@pytest.mark.asyncio
async def test_plan_follow_up_queries_zero_results():
    """When zero comps found but year range overlaps dataset, suggests dropping genre."""
    result = await plan_follow_up_queries(
        genres=["Sci-Fi", "Thriller"],
        year_start=1980,
        year_end=1990,
        rating_threshold=8.0,
        comps_found=0,
        comps_requested=5,
    )
    assert len(result["suggestions"]) > 0
    suggestion = result["suggestions"][0]
    assert suggestion["strategy"] == "drop_one_genre"


@pytest.mark.asyncio
async def test_plan_follow_up_queries_partial_results():
    """When some but not enough comps found, suggests lowering threshold."""
    result = await plan_follow_up_queries(
        genres=["Sci-Fi", "Thriller"],
        year_start=1980,
        year_end=1990,
        rating_threshold=8.0,
        comps_found=2,
        comps_requested=5,
    )
    assert len(result["suggestions"]) > 0
    suggestion = result["suggestions"][0]
    assert suggestion["strategy"] == "lower_threshold"


@pytest.mark.asyncio
async def test_plan_follow_up_queries_sufficient():
    """When enough comps found, no broadening needed."""
    result = await plan_follow_up_queries(
        genres=["Sci-Fi", "Thriller"],
        year_start=2000,
        year_end=2005,
        rating_threshold=7.0,
        comps_found=5,
        comps_requested=5,
    )
    assert len(result["suggestions"]) == 1
    assert result["suggestions"][0]["strategy"] == "sufficient_evidence"


@pytest.mark.asyncio
async def test_diagnose_query_failure_empty_year_outside_dataset():
    """Zero rows with year > 2008 should suggest using historical range."""
    sql = "SELECT * FROM imdb.movies WHERE year BETWEEN 2022 AND 2026"
    result = await diagnose_query_failure(sql=sql, empty_result=True)
    assert len(result["diagnosis"]) > 0
    assert "2008" in str(result["diagnosis"])
    recovery = result["recovery_suggestions"][0]
    assert recovery["action"] == "use_historical_range"


@pytest.mark.asyncio
async def test_diagnose_query_failure_error_column_not_found():
    """Error with unknown column should suggest checking schema."""
    sql = "SELECT title FROM imdb.movies WHERE rank > 7"
    error = "Unknown column 'title' does not exist"
    result = await diagnose_query_failure(sql=sql, error_message=error)
    assert len(result["diagnosis"]) > 0
    assert "title" in str(result["diagnosis"]).lower()
    recovery = result["recovery_suggestions"][0]
    assert recovery["action"] == "check_schema"


@pytest.mark.asyncio
async def test_diagnose_query_failure_error_table_not_found():
    """Error with table not found should suggest listing tables."""
    sql = "SELECT * FROM imdb.films"
    error = "Table 'imdb.films' does not exist"
    result = await diagnose_query_failure(sql=sql, error_message=error)
    assert len(result["diagnosis"]) > 0
    recovery = result["recovery_suggestions"][0]
    assert recovery["action"] == "call_list_tables"


@pytest.mark.asyncio
async def test_diagnose_query_failure_proceed_recommendation():
    """Should recommend retry when recovery suggestions exist, abort when not."""
    result_no_suggestions = await diagnose_query_failure(sql="SELECT 1", empty_result=True)
    assert result_no_suggestions["proceed_recommendation"] in ("retry_with_fixes", "abort_and_report")

    result_with_error = await diagnose_query_failure(
        sql="SELECT title FROM imdb.movies",
        error_message="Unknown column 'title' does not exist",
    )
    assert result_with_error["proceed_recommendation"] == "retry_with_fixes"


# ---------------------------------------------------------------------------
# generate_html_memo tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_html_memo_basic():
    result = await generate_html_memo(
        title="Dune: Part Two",
        recommendation="ACQUIRE",
        rationale="Strong genre average of 7.8 [ClickHouse IMDb]. Comparable titles show consistent performance.",
        comparable_titles=[{"title": "Comp A", "year": 2005, "rank": 8.0}],
        risk_flags=["Premium acquisition cost"],
        genre_benchmark=[{"genre": "Sci-Fi", "avg_rating": 7.2, "stddev": 0.5, "title_count": 120}],
        constraint_audit={
            "year_range_preserved": "YES",
            "rating_threshold_preserved": "YES",
            "both_genres_required": "YES",
            "max_comps_respected": "YES",
            "historical_fallback_separated": "YES",
            "no_fabricated_data": "YES",
            "synthetic_data_labeled": "YES",
            "post_2015_trend_supported": "NO",
        },
    )
    assert result["status"] == "success"
    assert result["recommendation"] == "ACQUIRE"
    assert "HTML memo" in result["message"]


@pytest.mark.asyncio
async def test_generate_html_memo_invalid_recommendation():
    result = await generate_html_memo(
        title="Test",
        recommendation="INVALID",
        rationale="Some rationale.",
        comparable_titles=[],
        risk_flags=[],
        genre_benchmark={},
    )
    assert result["status"] == "error"
    assert "recommendation must be one of" in result["message"]


@pytest.mark.asyncio
async def test_generate_html_memo_empty_comps():
    result = await generate_html_memo(
        title="Hypothetical Film",
        recommendation="FURTHER_REVIEW",
        rationale="Insufficient data. Dataset ends 2008.",
        comparable_titles=[],
        risk_flags=["No modern comps"],
        genre_benchmark=[{"genre": "Sci-Fi", "avg_rating": 7.2, "stddev": 0.5, "title_count": 120}],
        comparable_titles_status="Zero titles matched: year range outside dataset.",
    )
    assert result["status"] == "success"
    assert result["recommendation"] == "FURTHER_REVIEW"


@pytest.mark.asyncio
async def test_generate_html_memo_with_market_comps():
    result = await generate_html_memo(
        title="Test Film",
        recommendation="PASS",
        rationale="Weak genre signals. Comparable titles underperform baseline.",
        comparable_titles=[],
        risk_flags=[],
        genre_benchmark=[{"genre": "Drama", "avg_rating": 6.5, "stddev": 0.8, "title_count": 200}],
        market_performance_comps=[
            {"title": "comp a", "year": 2023, "platform": "Netflix", "streaming_views_m_first30": 10.0, "opening_week_usd_m": 5.0},
        ],
        constraint_audit={"no_fabricated_data": "YES"},
    )
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_generate_html_memo_with_director():
    result = await generate_html_memo(
        title="Director Film",
        recommendation="FURTHER_REVIEW",
        rationale="Director has limited track record in database.",
        comparable_titles=[],
        risk_flags=[],
        genre_benchmark=[{"genre": "Thriller", "avg_rating": 6.8, "stddev": 0.6, "title_count": 80}],
        director_analysis=[
            {"field": "Director", "value": "Christopher Nolan", "source": "[User-provided]"},
            {"field": "Track record", "value": "3 films in database", "source": "[ClickHouse IMDb]"},
        ],
    )
    assert result["status"] == "success"


# ---------------------------------------------------------------------------
# log_query_metadata tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_query_metadata():
    result = await log_query_metadata(
        query_id="Q1",
        sql="SELECT DISTINCT genre FROM imdb.genres",
        description="Genre string verification",
        rows_returned=5,
    )
    assert result["status"] == "success"
    assert result["query_id"] == "Q1"


@pytest.mark.asyncio
async def test_log_query_metadata_with_timing():
    result = await log_query_metadata(
        query_id="Q2",
        sql="SELECT g.genre, round(avg(m.rank),2) AS avg_rating FROM imdb.movies m JOIN imdb.genres g ON m.id = g.movie_id WHERE g.genre IN ('Sci-Fi','Thriller') AND m.rank > 0 GROUP BY g.genre",
        description="Genre benchmark",
        rows_returned=2,
        execution_time_ms=182,
    )
    assert result["status"] == "success"
    assert result["query_id"] == "Q2"