"""Tests for the full pipeline flow (end-to-end tool coordination)."""
import pytest

from screenscore.tools import (
    get_schema_info,
    validate_analysis_constraints,
    get_title_performance,
    generate_acquisition_memo,
    format_table,
    generate_chart,
)


@pytest.mark.asyncio
async def test_pipeline_schema_then_validation():
    """Simulate the beginning of the pipeline: schema → validation precondition."""
    schema = await get_schema_info()
    assert schema["status"] == "verified"

    year_start, year_end = 2022, 2026
    # Expect zero results since dataset ends in 2008
    validation = await validate_analysis_constraints(
        requested_year_start=year_start,
        requested_year_end=year_end,
        requested_rating_threshold=7.5,
        requested_genres=["Sci-Fi", "Thriller"],
        requested_max_comps=5,
        valid_comps_found=0,
        fallback_separate=True,
        fabricated_data=False,
    )
    assert validation["status"] == "PASS"


@pytest.mark.asyncio
async def test_pipeline_with_director_analysis():
    """Verify the director analysis table generation via memo."""
    result = await generate_acquisition_memo(
        title="Hypothetical Film",
        recommendation="FURTHER_REVIEW",
        rationale="Genre average of 7.2 [ClickHouse IMDb] provides a benchmark. "
                   "No director supplied, warranting further review.",
        comparable_titles=[],
        risk_flags=["No director analysis available"],
        genre_benchmark=[{"genre": "Sci-Fi", "avg_rating": 7.2, "stddev": 0.5, "title_count": 120}],
        comparable_titles_status="Zero titles matched.",
        director_analysis=[
            {"field": "Director", "value": "Not supplied in request", "source": "[User-provided — omitted]"},
            {"field": "Track record", "value": "Not evaluable — no director was supplied", "source": "[Unavailable]"},
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
    assert "Director" in result["memo_markdown"]


@pytest.mark.asyncio
async def test_pipeline_with_market_comps():
    """Verify synthetic market comps feeding into the memo."""
    oppenheimer = await get_title_performance("Oppenheimer")
    anora = await get_title_performance("Anora")
    assert oppenheimer["status"] == "found"
    assert anora["status"] == "found"

    result = await generate_acquisition_memo(
        title="Hypothetical Film",
        recommendation="FURTHER_REVIEW",
        rationale="Market comps show streaming performance range of "
                   f"{oppenheimer['streaming_views_m_first30']}M–{anora['streaming_views_m_first30']}M [Synthetic Benchmark].",
        comparable_titles=[],
        risk_flags=[],
        genre_benchmark=[{"genre": "Sci-Fi", "avg_rating": 7.2, "stddev": 0.5, "title_count": 120}],
        market_performance_comps=[oppenheimer, anora],
    )
    assert result["status"] == "success"
    assert "[Synthetic Benchmark]" in result["memo_markdown"]


@pytest.mark.asyncio
async def test_pipeline_format_chart_and_table():
    """Verify that table and chart tools work as supporting tools."""
    table = await format_table(
        headers=["Genre", "Avg Rating"],
        rows=[["Sci-Fi", "7.2"], ["Thriller", "6.8"]],
        title="Genre Benchmarks",
    )
    assert table["status"] == "success"

    chart = await generate_chart(
        chart_type="bar",
        title="Genre Comparison",
        x_label="Genre",
        y_label="Avg Rating",
        data_points=[{"label": "Sci-Fi", "value": 7.2}, {"label": "Thriller", "value": 6.8}],
    )
    assert chart["status"] == "success"


@pytest.mark.asyncio
async def test_pipeline_fabricated_data_blocked():
    """Fabricated data must cause validation to fail and block memo generation."""
    validation = await validate_analysis_constraints(
        requested_year_start=2022,
        requested_year_end=2026,
        requested_rating_threshold=7.5,
        requested_genres=["Sci-Fi"],
        requested_max_comps=5,
        valid_comps_found=0,
        fallback_separate=True,
        fabricated_data=True,
    )
    assert validation["status"] == "FAIL"
    assert validation["proceed_to_memo"] is False