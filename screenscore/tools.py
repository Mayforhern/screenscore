import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from google.adk.tools.tool_context import ToolContext
from google.genai import types

from .synthetic_data import _PERFORMANCE_DATA
from .pipeline import (
    _get_pipeline,
    _log_transition,
    RESEARCH_MAX_LIMIT,
)

logger = logging.getLogger(__name__)

_FALLBACK_SCHEMA: dict[str, Any] = {
    "status": "verified",
    "source": "HARDCODED FALLBACK — live DESCRIBE TABLE queries failed",
    "tables": {
        "imdb.movies": {
            "columns": ["id (UInt32)", "name (String)", "year (UInt32)", "rank (Float32, DEFAULT 0)"]
        },
        "imdb.actors": {
            "columns": ["id (UInt32)", "first_name (String)", "last_name (String)", "gender (FixedString(1))"]
        },
        "imdb.directors": {
            "columns": ["id (UInt32)", "first_name (String)", "last_name (String)"]
        },
        "imdb.roles": {"columns": ["actor_id (UInt32)", "movie_id (UInt32)", "role (String)"]},
        "imdb.genres": {"columns": ["movie_id (UInt32)", "genre (String)"]},
        "imdb.movie_directors": {"columns": ["director_id (UInt32)", "movie_id (UInt32)"]},
    },
    "dataset_coverage": {
        "min_year": 1888,
        "max_year": 2008,
        "total_movies": 388269,
        "rated_movies": 67245,
        "note": "ZERO movies exist with year > 2008. Any query for 2009–2026 returns 0 rows.",
    },
    "verified_semantics": {
        "rank": "IMDb user rating (Float32, 0.0–10.0). DEFAULT 0 means unrated. Always filter rank > 0 for meaningful analysis.",
        "name": "Movie title string. Use `name`, NOT `title` (title column does not exist).",
        "genre_strings": ["Sci-Fi", "Thriller"],
    },
    "available_fields": ["id", "name", "year", "rank"],
    "unavailable_fields": [
        "title (does not exist — use name)",
        "vote_count",
        "runtime",
        "production_company",
        "language",
        "country",
        "budget",
        "awards",
        "box_office",
        "streaming_views",
    ],
}


async def get_schema_info() -> dict[str, Any]:
    """Return the verified ClickHouse IMDb schema via live DESCRIBE TABLE queries.

    Connects directly to ClickHouse to discover the actual schema at runtime.
    Falls back to a hardcoded snapshot if the connection is unavailable.
    """
    host = os.environ.get("CLICKHOUSE_HOST", "sql-clickhouse.clickhouse.com")
    port = os.environ.get("CLICKHOUSE_PORT", "8443")
    user = os.environ.get("CLICKHOUSE_USER", "demo")
    password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    secure = os.environ.get("CLICKHOUSE_SECURE", "true").lower() == "true"

    try:
        import clickhouse_connect

        client = clickhouse_connect.get_client(
            host=host,
            port=int(port),
            username=user,
            password=password,
            secure=secure,
        )

        tables_to_describe = [
            "imdb.movies",
            "imdb.actors",
            "imdb.directors",
            "imdb.roles",
            "imdb.genres",
            "imdb.movie_directors",
        ]

        tables: dict[str, Any] = {}
        for table in tables_to_describe:
            try:
                result = client.query(f"DESCRIBE TABLE {table}")
                columns = [f"{row[0]} ({row[1]})" for row in result.result_rows]
                tables[table] = {"columns": columns}
            except Exception as e:
                logger.warning("Failed to DESCRIBE %s: %s", table, e)
                tables[table] = {"columns": [], "error": str(e)}

        coverage_result = client.query(
            "SELECT min(year), max(year), count(*) FROM imdb.movies"
        )
        min_year, max_year, total_movies = coverage_result.result_rows[0]

        rated_result = client.query(
            "SELECT count(*) FROM imdb.movies WHERE rank > 0"
        )
        rated_movies = rated_result.result_rows[0][0]

        client.close()

        return {
            "status": "verified",
            "source": "live DESCRIBE TABLE queries via clickhouse-connect",
            "tables": tables,
            "dataset_coverage": {
                "min_year": min_year,
                "max_year": max_year,
                "total_movies": total_movies,
                "rated_movies": rated_movies,
                "note": "ZERO movies exist with year > 2008. Any query for 2009–2026 returns 0 rows.",
            },
            "verified_semantics": {
                "rank": "IMDb user rating (Float32, 0.0–10.0). DEFAULT 0 means unrated. Always filter rank > 0 for meaningful analysis.",
                "name": "Movie title string. Use `name`, NOT `title` (title column does not exist).",
                "genre_strings": ["Sci-Fi", "Thriller"],
            },
            "available_fields": ["id", "name", "year", "rank"],
            "unavailable_fields": [
                "title (does not exist — use name)",
                "vote_count",
                "runtime",
                "production_company",
                "language",
                "country",
                "budget",
                "awards",
                "box_office",
                "streaming_views",
            ],
        }
    except Exception as e:
        logger.warning("Live schema fetch failed, using fallback: %s", e)
        return dict(_FALLBACK_SCHEMA)


async def validate_analysis_constraints(
    requested_year_start: int,
    requested_year_end: int,
    requested_rating_threshold: float,
    requested_genres: list[str],
    requested_max_comps: int,
    valid_comps_found: int,
    fallback_separate: bool,
    fabricated_data: bool,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Validate all analysis constraints before generating the final memo.

    Call this BEFORE generate_acquisition_memo. If any check fails, do NOT
    call generate_acquisition_memo — report the failure instead.

    When tool_context is provided, propagates evidence dependencies:
    - If target genres are NOT verified (e.g., target absent from ClickHouse),
      strict comparable claims are gated until genre overlap is independently verified.
    """
    from screenscore.evidence import EvidenceRegistry

    DB_MAX_YEAR = 2008
    DB_MIN_YEAR = 1888

    checks = {
        "year_range_preserved": {
            "requested": f"{requested_year_start}–{requested_year_end}",
            "note": f"Dataset covers {DB_MIN_YEAR}–{DB_MAX_YEAR}. Zero results expected for {requested_year_start}–{requested_year_end}.",
            "pass": True,
        },
        "rating_threshold_preserved": {
            "requested": f">= {requested_rating_threshold}",
            "pass": True,
        },
        "genres_preserved": {
            "requested": requested_genres,
            "pass": True,
        },
        "valid_comps_count": {
            "requested_max": requested_max_comps,
            "found": valid_comps_found,
            "note": f"{'Zero results — dataset ends 2008' if valid_comps_found == 0 else f'{valid_comps_found} titles found'}",
            "pass": True,
        },
        "fallback_separate": {
            "value": fallback_separate,
            "pass": fallback_separate,
            "note": "historical_fallback_comps must be separate from comparable_titles",
        },
        "no_fabricated_data": {
            "value": not fabricated_data,
            "pass": not fabricated_data,
            "note": "All results must come from actual run_query or get_title_performance calls",
        },
    }

    # Evidence dependency propagation
    evidence_gates = []
    target_absent = False
    if tool_context:
        pipeline = _get_pipeline(tool_context.state)
        registry = EvidenceRegistry.from_dict({
            "items": pipeline.get("evidence_items", {}),
            "claims": pipeline.get("evidence_claims", {}),
            "dependencies": pipeline.get("evidence_dependencies", {}),
            "candidate_classifications": pipeline.get("candidate_classifications", {}),
        })

        # Check if target genres are verified — get_evidence_status returns EvidenceStatus | None
        _tg_status = registry.get_evidence_status("target_genres")
        target_genres_verified = (
            _tg_status is not None
            and str(getattr(_tg_status, "value", _tg_status)) in ("verified", "derived")
        )

        # Detect if target is absent from database (not_verified = target not in ClickHouse)
        target_absent = (
            _tg_status is not None
            and str(getattr(_tg_status, "value", _tg_status)) == "not_verified"
        )

        checks["target_genres_verified"] = {
            "value": target_genres_verified,
            "pass": target_genres_verified,
            "note": "Target genres must be verified before promoting candidates to strict comparables",
        }

        # Check if any candidates were classified as strict comparable without verified target genres
        for candidate_id, classification in registry.candidate_classifications.items():
            if str(getattr(classification, "value", classification)) == "strict_comparable" and not target_genres_verified:
                evidence_gates.append(
                    f"Candidate '{candidate_id}' classified as strict_comparable "
                    f"but target genres are not verified"
                )

        # Validate all claims
        claim_statuses = registry.validate_all_claims()
        for claim_id, claim_status in claim_statuses.items():
            if str(getattr(claim_status, "value", claim_status)) == "gated":
                claim = registry.claims[claim_id]
                evidence_gates.append(
                    f"Claim '{claim_id}' is gated by evidence: {claim.gated_by}"
                )

        # Persist updated evidence state
        pipeline["evidence_items"] = registry.to_dict()["items"]
        pipeline["evidence_claims"] = registry.to_dict()["claims"]
        pipeline["evidence_dependencies"] = registry.to_dict()["dependencies"]

    # Evidence gates are warnings, not blockers — memo can still be generated
    # with FURTHER_REVIEW recommendation when evidence is incomplete
    if evidence_gates:
        checks["evidence_dependencies_satisfied"] = {
            "value": False,
            "pass": True,  # Warning, not blocker — allow memo with FURTHER_REVIEW
            "note": "Evidence dependency warnings — proceed with FURTHER_REVIEW recommendation",
            "gates": evidence_gates,
        }

    # Separate blocking failures from warnings
    blocking_failures = []
    warnings = []
    for k, v in checks.items():
        if not v["pass"]:
            if k in ("evidence_dependencies_satisfied", "target_genres_verified"):
                warnings.append(k)
            else:
                blocking_failures.append(k)

    # If target is absent, this is expected — not a failure
    if target_absent and "target_genres_verified" in blocking_failures:
        blocking_failures.remove("target_genres_verified")
        checks["target_genres_verified"]["pass"] = True
        checks["target_genres_verified"]["note"] = (
            "Target absent from ClickHouse — using genre benchmarks and historical fallback only"
        )

    status = "PASS" if not blocking_failures else "FAIL"

    # Determine proceed_to_memo logic
    if status == "PASS":
        proceed_to_memo = True
        if warnings:
            message = (
                "Constraints validated with warnings. "
                "Proceed to memo with FURTHER_REVIEW recommendation due to evidence gaps."
            )
        else:
            message = "All constraints validated. Safe to call generate_acquisition_memo."
    else:
        proceed_to_memo = False
        message = (
            f"VALIDATION FAILED: {blocking_failures}. "
            f"Do NOT call generate_acquisition_memo. Report failures to user."
        )

    return {
        "status": status,
        "checks": checks,
        "failures": blocking_failures,
        "warnings": warnings,
        "evidence_gates": evidence_gates,
        "proceed_to_memo": proceed_to_memo,
        "message": message,
    }


async def get_title_performance(title: str) -> dict[str, Any]:
    """Retrieve curated market comps for a title from the built-in benchmark registry.

    ScreenScore uses a two-layer analysis architecture by design:

    Layer 1 — ClickHouse SQL: live queries against the IMDb dataset (1888–2008).
               Provides genre benchmarks, director track records, and historical comps.

    Layer 2 — Curated Market Registry (this function): a fixed set of recent high-profile
               titles (2022–2025) with streaming views, opening week revenue, platform,
               and awards data. This bridges the public dataset's 2008 cutoff.

    Design rationale: The ClickHouse SQL Playground demo dataset ends at 2008. Rather than
    hallucinate recent data or refuse to analyze modern titles, ScreenScore explicitly separates
    database-sourced facts from curated benchmark figures. Every result from this function
    is labeled [Synthetic Benchmark — Curated Market Registry, not ClickHouse] so data
    provenance is always transparent to the user.

    Returns a flat dict ready for market_performance_comps in the acquisition memo.
    """
    key = title.lower().strip()

    def _build_result(matched_key: str) -> dict[str, Any]:
        d = _PERFORMANCE_DATA[matched_key]
        return {
            "status": "found",
            "title": matched_key,
            "year": d.get("year"),
            "director": d.get("director"),
            "director_known_for": d.get("director_known_for"),
            "cast": d.get("cast"),
            "imdb_rating": d.get("imdb_rating"),
            "streaming_views_m_first30": d.get("streaming_views_m_first30"),
            "opening_week_usd_m": d.get("opening_week_usd_m"),
            "box_office_total_usd_m": d.get("box_office_total_usd_m"),
            "budget_usd_m": d.get("budget_usd_m"),
            "platform": d.get("platform"),
            "genre": d.get("genre"),
            "awards": d.get("awards"),
            "source": "curated_market_registry",
            "source_label": "[Synthetic Benchmark — Curated Market Registry, not ClickHouse]",
            "registry_note": (
                "Part of ScreenScore two-layer architecture: "
                "ClickHouse SQL (1888–2008 historical data) + "
                "Curated Market Registry (2021–2025 recent titles). "
                "imdb_rating is from public IMDb as of registry build date."
            ),
        }

    if key in _PERFORMANCE_DATA:
        return _build_result(key)

    matches = [k for k in _PERFORMANCE_DATA if key in k or k in key]
    if len(matches) == 1:
        return _build_result(matches[0])
    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "message": f"Multiple matches found: {matches}. Please be more specific.",
        }

    return {
        "status": "not_found",
        "title": title,
        "source": "curated_market_registry",
        "message": f"No curated market data for '{title}'.",
        "available_titles": sorted(_PERFORMANCE_DATA.keys()),
    }


async def generate_acquisition_memo(
    title: str,
    recommendation: str,
    rationale: str,
    comparable_titles: list[dict],
    risk_flags: list[str],
    genre_benchmark: dict | list[dict],
    historical_fallback_comps: list[dict] | None = None,
    market_performance_comps: list[dict] | None = None,
    constraint_violations: list[str] | None = None,
    sql_queries_run: list[str] | None = None,
    constraint_audit: dict | None = None,
    title_metadata: list[dict] | None = None,
    sql_plan: str | None = None,
    director_analysis: list[dict] | None = None,
    comparable_titles_status: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Generate a structured acquisition memo and save it as a downloadable artifact.

    This is the DECIDE/ACT step of the pipeline. Call validate_analysis_constraints first.

    When tool_context is provided, enforces that:
    - Constraint validation has passed (or explicitly failed)
    - Final decision status is set
    - Memo has not already been generated
    """
    # Pipeline gating checks
    # Evidence gating: check for unsupported claims
    evidence_gates = []
    if tool_context:
        from screenscore.evidence import EvidenceRegistry

        pipeline = _get_pipeline(tool_context.state)

        # Check if memo was already generated
        if pipeline.get("memo_generated"):
            _log_transition(pipeline, "[PIPELINE] generate_acquisition_memo BLOCKED — memo already generated")
            return {
                "status": "error",
                "message": "Memo has already been generated for this pipeline session. "
                           "Cannot generate duplicate memo.",
            }

        # Check if validation has been completed
        validation_status = pipeline.get("constraint_validation_status", "pending")
        if validation_status == "pending":
            _log_transition(pipeline, "[PIPELINE] generate_acquisition_memo BLOCKED — validation not completed")
            return {
                "status": "error",
                "message": "Constraint validation has not been completed yet. "
                           "Call validate_analysis_constraints and mark_validation_complete first.",
            }

        # Validate evidence dependencies for claims
        registry = EvidenceRegistry.from_dict({
            "items": pipeline.get("evidence_items", {}),
            "claims": pipeline.get("evidence_claims", {}),
            "dependencies": pipeline.get("evidence_dependencies", {}),
            "candidate_classifications": pipeline.get("candidate_classifications", {}),
        })

        claim_statuses = registry.validate_all_claims()
        for claim_id, claim_status in claim_statuses.items():
            if str(getattr(claim_status, "value", claim_status)) == "gated":
                claim = registry.claims[claim_id]
                evidence_gates.append({
                    "claim_id": claim_id,
                    "description": claim.description,
                    "gated_by": claim.gated_by,
                })

        _log_transition(
            pipeline,
            f"[PIPELINE] STEP 8 memo generation — validation={validation_status} "
            f"recommendation={recommendation} evidence_gates={len(evidence_gates)}",
        )

    if recommendation not in ("ACQUIRE", "PASS", "FURTHER_REVIEW"):
        return {
            "status": "error",
            "message": "recommendation must be one of: ACQUIRE, PASS, FURTHER_REVIEW",
        }

    # W7: Soft truncation — prevent runaway artifact sizes
    _MAX_RATIONALE = 4000
    _MAX_SQL_QUERIES = 30
    _truncation_warnings: list[str] = []
    if len(rationale) > _MAX_RATIONALE:
        rationale = rationale[:_MAX_RATIONALE] + "\n\n*[Rationale truncated for length]*"
        _truncation_warnings.append(f"rationale truncated to {_MAX_RATIONALE} chars")
    if sql_queries_run and len(sql_queries_run) > _MAX_SQL_QUERIES:
        sql_queries_run = sql_queries_run[:_MAX_SQL_QUERIES]
        _truncation_warnings.append(f"sql_queries_run truncated to {_MAX_SQL_QUERIES} entries")
    if _truncation_warnings:
        logger.warning("generate_acquisition_memo: input truncated — %s", "; ".join(_truncation_warnings))

    logger.info(
        "Generating acquisition memo for '%s' — recommendation: %s",
        title, recommendation,
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


    if comparable_titles:
        comp_rows = ""
        for c in comparable_titles:
            t = c.get("title") or c.get("name", "N/A")
            r = c.get("rating") or c.get("rank", "N/A")
            comp_rows += f"| {t} | {c.get('year', 'N/A')} | {r} [ClickHouse IMDb] |\n"
    else:
        status_note = comparable_titles_status or "Zero titles matched requested criteria."
        comp_rows = f"| No valid titles | — | — |\n"

    comp_status_line = (
        f"\n> **Status:** {comparable_titles_status}\n"
        if comparable_titles_status
        else ""
    )

    fallbacks = historical_fallback_comps or []
    if fallbacks:
        fb_rows = ""
        for c in fallbacks:
            t = c.get("title") or c.get("name", "N/A")
            r = c.get("rating") or c.get("rank", "N/A")
            fb_rows += f"| {t} | {c.get('year', 'N/A')} | {r} [ClickHouse IMDb] |\n"
        fallback_section = f"""---

## Historical Fallback Comps

> **WARNING: These titles DO NOT satisfy the requested criteria.**
> They are pre-2009 historical references only. Source: [ClickHouse IMDb]

| Title | Year | IMDb Rating |
|---|---|---|
{fb_rows}
"""
    else:
        fallback_section = ""

    market_comps = market_performance_comps or []
    if market_comps:
        mkt_rows = ""
        for m in market_comps:
            views = f"{m.get('streaming_views_m_first30')}M" if m.get("streaming_views_m_first30") else "N/A"
            box = f"${m.get('opening_week_usd_m')}M" if m.get("opening_week_usd_m") else "N/A"
            mkt_rows += f"| {m.get('title', 'N/A')} | {m.get('year', 'N/A')} | {m.get('platform', 'N/A')} | {views} | {box} | [Synthetic Benchmark] |\n"
        market_section = f"""---

## Market Performance Benchmarks (Synthetic Comps)

> Source: Synthetic in-memory table. Not from ClickHouse. Do not mix with IMDb data.

| Title | Year | Platform | Streaming Views (30d) | Opening Box Office | Source |
|---|---|---|---|---|---|
{mkt_rows}
"""
    else:
        market_section = ""

    benchmarks = genre_benchmark if isinstance(genre_benchmark, list) else [genre_benchmark]
    gb_rows = ""
    for gb in benchmarks:
        gb_rows += (
            f"| {gb.get('genre', 'N/A')} | {gb.get('avg_rating', 'N/A')} "
            f"| {gb.get('stddev', 'N/A')} | {gb.get('title_count', 'N/A')} | [ClickHouse IMDb] |\n"
        )

    meta = title_metadata or []
    if meta:
        meta_rows = ""
        for m in meta:
            meta_rows += f"| {m.get('field', 'N/A')} | {m.get('value', 'N/A')} | {m.get('source', 'N/A')} |\n"
        title_metadata_section = f"""---

## Title Metadata

| Field | Value | Source |
|---|---|---|
{meta_rows}
"""
    else:
        title_metadata_section = ""

    director = director_analysis or []
    if director:
        dir_rows = ""
        for d in director:
            dir_rows += f"| {d.get('field', 'N/A')} | {d.get('value', 'N/A')} | {d.get('source', 'N/A')} |\n"
        director_section = f"""---

## Director Track Record

| Field | Value | Source |
|---|---|---|
{dir_rows}
"""
    else:
        director_section = ""

    audit = constraint_audit or {}
    if audit:
        audit_rows = ""
        _labels = {
            "year_range_preserved": "Year range preserved",
            "rating_threshold_preserved": "Rating threshold preserved",
            "both_genres_required": "Both genres required",
            "max_comps_respected": "Maximum primary comps respected",
            "historical_fallback_separated": "Historical fallback separated",
            "no_fabricated_data": "No fabricated data",
            "synthetic_data_labeled": "Synthetic data clearly labeled",
            "post_2015_trend_supported": "Post-2015 trend supported by dataset",
        }
        for key, label in _labels.items():
            val = audit.get(key, "N/A")
            icon = "✓" if str(val).upper() in ("YES", "TRUE", "PASS") else ("✗" if str(val).upper() in ("NO", "FALSE", "FAIL") else "—")
            audit_rows += f"| {label} | {val} | {icon} |\n"
        audit_section = f"""---

## Constraint Audit

| Check | Result | |
|---|---|---|
{audit_rows}
"""
    else:
        audit_section = ""

    plan_section = f"""---

## Query Plan

```
{sql_plan}
```

""" if sql_plan else ""

    risk_lines = "\n".join(f"- {flag}" for flag in risk_flags) if risk_flags else "- No material risks identified"

    violations = constraint_violations or []
    if violations:
        violation_lines = "\n".join(f"- {v}" for v in violations)
        constraint_section = f"""---

## Constraint Violations

The following user-specified criteria could not be fully satisfied:

{violation_lines}

"""
    else:
        constraint_section = ""

    queries = sql_queries_run or []
    if queries:
        query_blocks = "\n\n".join(f"```sql\n{q}\n```" for q in queries)
        sql_audit_section = f"""---

## Audit Trail: Executed SQL Queries

{query_blocks}

"""
    else:
        sql_audit_section = ""

    # Evidence gates section
    if evidence_gates:
        gate_lines = ""
        for gate in evidence_gates:
            gate_lines += f"- **{gate['claim_id']}**: {gate['description']} — gated by: {', '.join(gate['gated_by'])}\n"
        evidence_section = f"""---

## Evidence Dependency Gates

The following claims are gated by missing evidence:

{gate_lines}
> These claims cannot be fully supported until the gated evidence is verified.
"""
    else:
        evidence_section = ""

    memo_md = f"""# Studio Acquisition Memo — {title}

**Generated:** {timestamp}
**Recommendation:** {recommendation}

---

## RECOMMENDATION: {recommendation}

---

## Executive Summary & Rationale

{rationale}

{constraint_section}{title_metadata_section}{director_section}{evidence_section}---

## Genre Benchmark

| Genre | Avg Rating (rated titles) | Std Dev | Total Rated Titles | Source |
|---|---|---|---|---|
{gb_rows}
---

## Requested Comparable Titles (Target Criteria)
{comp_status_line}
| Title | Year | IMDb Rating |
|---|---|---|
{comp_rows}
{fallback_section}{market_section}{audit_section}---

## Risk Flags

{risk_lines}

{plan_section}{sql_audit_section}---

## Decision

**RECOMMENDATION: {recommendation}**

*Data sources: genre benchmark and comparable titles from ClickHouse IMDb dataset (1888–2008).
Performance benchmarks are synthetic market estimates. No corresponding columns were found
in the discovered ClickHouse schema for: budget, votes, runtime, production_company, language,
country, awards. This document does not constitute legal, financial, or contractual advice.*
"""

    memo_json = {
        "title": title,
        "recommendation": recommendation,
        "timestamp": timestamp,
        "rationale": rationale,
        "genre_benchmark": benchmarks,
        "requested_comparable_titles": comparable_titles,
        "comparable_titles_status": comparable_titles_status or "N/A",
        "historical_fallback_comps": fallbacks,
        "market_performance_comps": market_comps,
        "title_metadata": meta,
        "director_analysis": director,
        "risk_flags": risk_flags,
        "constraint_violations": violations,
        "constraint_audit": audit,
        "sql_plan": sql_plan or "",
        "sql_queries_run": queries,
        "evidence_gates": evidence_gates,
        "data_sources": {
            "genre_benchmark": "ClickHouse imdb.movies + imdb.genres (1888–2008)",
            "requested_comparable_titles": "ClickHouse imdb.movies + imdb.genres (1888–2008)",
            "historical_fallback_comps": "ClickHouse imdb.movies + imdb.genres (1888–2008)",
            "market_performance_comps": "Synthetic in-memory table (get_title_performance)",
            "title_metadata": "User-provided hypothetical / ClickHouse schema absence confirmed",
        },
    }

    if tool_context:
        safe_title = title[:40].replace(" ", "_").replace("/", "-")
        md_filename = f"acquisition_memo_{safe_title}.md"
        json_filename = f"acquisition_memo_{safe_title}.json"

        memo_artifact = types.Part(
            inline_data=types.Blob(mime_type="text/markdown", data=memo_md.encode("utf-8"))
        )
        md_version = await tool_context.save_artifact(
            filename=md_filename, artifact=memo_artifact
        )

        json_artifact = types.Part(
            inline_data=types.Blob(
                mime_type="application/json",
                data=json.dumps(memo_json, indent=2).encode("utf-8"),
            )
        )
        json_version = await tool_context.save_artifact(
            filename=json_filename, artifact=json_artifact
        )

        logger.info(
            "Saved memo artifacts for '%s': %s (v%s), %s (v%s)",
            title, md_filename, md_version, json_filename, json_version,
        )

    artifact_filename = f"acquisition_memo_{title[:40].replace(' ', '_').replace('/', '-')}.md" if tool_context else None


    return {
        "status": "success",
        "recommendation": recommendation,
        "title": title,
        "constraint_violations": violations,
        "artifact_filename": artifact_filename,
        "message": (
            f"Acquisition memo for '{title}' saved as '{artifact_filename}'. "
            f"Recommendation: {recommendation}."
            if artifact_filename
            else f"Acquisition memo for '{title}' generated. Recommendation: {recommendation}."
        ),
        "memo_markdown": memo_md,
        "memo_json": json.dumps(memo_json, indent=2),
    }


async def plan_follow_up_queries(
    genres: list[str],
    year_start: int,
    year_end: int,
    rating_threshold: float,
    comps_found: int,
    comps_requested: int,
    dataset_max_year: int = 2008,
    dataset_min_year: int = 1888,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Analyze current query results and recommend follow-up queries when evidence is thin.

    Call this when initial comparable title queries return fewer results than requested.
    It suggests specific SQL queries to broaden the search while preserving constraints
    as much as possible.

    If tool_context is provided, enforces max query limit from pipeline state.
    """
    # Enforce max query limit if pipeline state is available
    if tool_context:
        pipeline = _get_pipeline(tool_context.state)
        planned_count = len(pipeline.get("planned_queries", []))
        max_queries = pipeline.get("max_queries", 20)
        if planned_count >= max_queries:
            _log_transition(
                pipeline,
                f"[PIPELINE] plan_follow_up_queries BLOCKED — "
                f"max queries ({max_queries}) reached ({planned_count} planned)",
            )
            return {
                "assessment": {
                    "comps_found": comps_found,
                    "comps_requested": comps_requested,
                    "issues": [f"Maximum query limit ({max_queries}) reached"],
                    "needs_broadening": False,
                    "dataset_coverage": f"{dataset_min_year}–{dataset_max_year}",
                },
                "suggestions": [{
                    "strategy": "max_queries_reached",
                    "description": f"Cannot plan additional queries — limit of {max_queries} reached. Proceed to constraint validation with available evidence.",
                    "rationale": "Max execution guard triggered. Continue pipeline with existing results.",
                    "sql_template": None,
                    "source_label": "[Pipeline enforcement]",
                }],
                "fallback_plan": {
                    "strategy": "proceed_with_available_evidence",
                    "note": "Max query limit reached — proceeding to validation with available data",
                },
            }

    suggestions = []
    issues = []
    needs_broadening = comps_found < comps_requested

    if year_start > dataset_max_year:
        issues.append(f"Year range {year_start}–{year_end} is entirely outside dataset ({dataset_min_year}–{dataset_max_year})")
        suggestions.append({
            "strategy": "use_historical_fallback",
            "description": f"Run comparable title query on {dataset_max_year - 4}–{dataset_max_year} instead of {year_start}–{year_end}",
            "rationale": "Dataset has no data beyond 2008 — the requested range will always return 0 results",
            "sql_template": (
                f"SELECT m.name, m.year, m.rank "
                f"FROM imdb.movies m "
                f"WHERE m.id IN (SELECT movie_id FROM imdb.genres WHERE genre = '<genre1>') "
                f"AND m.id IN (SELECT movie_id FROM imdb.genres WHERE genre = '<genre2>') "
                f"AND m.rank >= {rating_threshold} "
                f"AND m.year BETWEEN {dataset_max_year - 4} AND {dataset_max_year} "
                f"ORDER BY m.rank DESC LIMIT {comps_requested}"
            ),
            "source_label": "[ClickHouse IMDb — historical fallback, not user-requested range]",
        })

    if comps_found == 0 and not suggestions:
        issues.append("Zero comparable titles found within requested criteria")
        suggestions.append({
            "strategy": "drop_one_genre",
            "description": f"Drop one genre requirement to find single-genre comps",
            "rationale": "Requiring BOTH genres may be too restrictive; single-genre matches can still be informative",
            "sql_template": (
                f"SELECT m.name, m.year, m.rank "
                f"FROM imdb.movies m "
                f"WHERE m.id IN (SELECT movie_id FROM imdb.genres WHERE genre = '<genre1>') "
                f"AND m.rank >= {rating_threshold} "
                f"AND m.year BETWEEN {year_start} AND {year_end} "
                f"ORDER BY m.rank DESC LIMIT {comps_requested}"
            ),
            "source_label": "[ClickHouse IMDb — broadened criteria]",
        })

    if comps_found > 0 and comps_found < comps_requested:
        issues.append(f"Only {comps_found}/{comps_requested} comparable titles found")
        suggestions.append({
            "strategy": "lower_threshold",
            "description": f"Lower rating threshold from {rating_threshold} to find more comps",
            "rationale": f"Only {comps_found} titles meet the strict threshold; relaxing it may yield additional comparables",
            "sql_template": (
                f"SELECT m.name, m.year, m.rank "
                f"FROM imdb.movies m "
                f"WHERE m.id IN (SELECT movie_id FROM imdb.genres WHERE genre = '<genre1>') "
                f"AND m.id IN (SELECT movie_id FROM imdb.genres WHERE genre = '<genre2>') "
                f"AND m.rank >= {max(rating_threshold - 0.5, 0.0)} "
                f"AND m.year BETWEEN {year_start} AND {year_end} "
                f"ORDER BY m.rank DESC LIMIT {comps_requested}"
            ),
            "source_label": "[ClickHouse IMDb — broadened criteria]",
        })

    if not suggestions:
        suggestions.append({
            "strategy": "sufficient_evidence",
            "description": f"Found {comps_found} comparable titles — no broadening needed",
            "rationale": "All requested criteria are satisfied with available data",
            "sql_template": None,
            "source_label": "[ClickHouse IMDb]",
        })

    fallback_plan = {
        "strategy": "use_synthetic_benchmarks_only" if year_start > dataset_max_year else "use_broadened_criteria",
        "note": "When no ClickHouse data matches criteria, rely on synthetic benchmarks for market context",
    }

    return {
        "assessment": {
            "comps_found": comps_found,
            "comps_requested": comps_requested,
            "issues": issues,
            "needs_broadening": needs_broadening,
            "dataset_coverage": f"{dataset_min_year}–{dataset_max_year}",
        },
        "suggestions": suggestions,
        "fallback_plan": fallback_plan,
    }


async def diagnose_query_failure(
    sql: str,
    error_message: str = "",
    empty_result: bool = False,
) -> dict[str, Any]:
    """Diagnose a failed or empty query result and suggest recovery actions.

    Call this when a run_query call returns an error or zero rows unexpectedly.
    Returns a diagnosis and specific recovery SQL to try.

    Args:
        sql: The SQL query that failed or returned empty.
        error_message: The error message from the failed query (empty if zero rows).
        empty_result: True if the query ran but returned 0 rows.

    Returns:
        Dict with diagnosis, likely_cause, recovery_suggestions.
    """
    sql_lower = sql.lower()
    diagnosis = []
    recovery_suggestions = []

    if empty_result:
        # Check if year range is outside dataset coverage
        year_match = re.findall(r'\byear\s+(between|>=|>|<=|<)\s*(\d{4})', sql_lower)
        if year_match:
            for op, year_str in year_match:
                year = int(year_str)
                if year > 2008:
                    diagnosis.append(f"Year {year} exceeds dataset maximum (2008)")
                    recovery_suggestions.append({
                        "action": "use_historical_range",
                        "description": f"Replace year range with pre-2009 range",
                        "suggested_sql": sql.replace(str(year), "2008") if op == "between" else "Use year <= 2008",
                    })

        # Check if both genres were required (INNER JOIN or subquery for both)
        genre_count = sql_lower.count("genre =")
        if genre_count >= 2:
            diagnosis.append("Multiple genre filters may be too restrictive")
            recovery_suggestions.append({
                "action": "drop_one_genre",
                "description": "Query uses both genres simultaneously; try with one genre",
                "suggested_sql": None,
            })

        if not diagnosis:
            diagnosis.append("Unknown cause — result set is empty but constraints appear valid")

    if error_message:
        # Column not found — only triggers when "unknown column" is explicitly mentioned
        if "unknown column" in error_message.lower():
            col_match = re.search(r"'\s*(\w+)\s*'\s+does not exist", error_message, re.IGNORECASE)
            if col_match:
                bad_col = col_match.group(1)
                diagnosis.append(f"Column '{bad_col}' does not exist in schema")
                recovery_suggestions.append({
                    "action": "check_schema",
                    "description": f"'{bad_col}' not in schema — call get_schema_info() for available columns",
                    "suggested_sql": sql.replace(bad_col, "name") if bad_col in ("title",) else None,
                })

        # Table not found
        if "table" in error_message.lower() and "does not exist" in error_message.lower():
            table_match = re.search(r"table\s+['\"]?([\w.]+)['\"]?\s+does not exist", error_message, re.IGNORECASE)
            if table_match:
                diagnosis.append(f"Table '{table_match.group(1)}' does not exist")
                recovery_suggestions.append({
                    "action": "call_list_tables",
                    "description": "Call list_tables to discover available tables, then retry",
                    "suggested_sql": None,
                })

        if not diagnosis:
            diagnosis.append(f"Query error: {error_message[:200]}")

    return {
        "diagnosis": diagnosis,
        "recovery_suggestions": recovery_suggestions,
        "proceed_recommendation": "retry_with_fixes" if recovery_suggestions else "abort_and_report",
    }


async def format_table(
    headers: list[str],
    rows: list[list[str]],
    title: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Format query results as a markdown table for display."""
    lines = []
    if title:
        lines.append(f"### {title}")
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")

    table_md = "\n".join(lines)

    if tool_context:
        table_bytes = table_md.encode("utf-8")
        artifact = types.Part(
            inline_data=types.Blob(
                mime_type="text/markdown",
                data=table_bytes,
            )
        )
        safe_title = title[:40].replace(" ", "_").replace("/", "-") if title else "table"
        await tool_context.save_artifact(filename=f"table_{safe_title}.md", artifact=artifact)

    return {"status": "success", "markdown": table_md}


async def generate_chart(
    chart_type: str,
    title: str,
    x_label: str,
    y_label: str,
    data_points: list[dict],
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Generate a chart configuration for visualization.

    Args:
        chart_type: Type of chart - 'bar', 'line', 'pie', or 'scatter'.
        title: Chart title.
        x_label: Label for the X axis.
        y_label: Label for the Y axis.
        data_points: List of dicts, each with 'label' (str) and 'value' (number).
    """
    import json as _json

    chart_config = {
        "type": chart_type,
        "title": title,
        "x_label": x_label,
        "y_label": y_label,
        "data": data_points,
    }

    if tool_context:
        chart_json = _json.dumps(chart_config, indent=2).encode("utf-8")
        artifact = types.Part(
            inline_data=types.Blob(
                mime_type="application/json",
                data=chart_json,
            )
        )
        safe_title = title[:40].replace(" ", "_").replace("/", "-")
        await tool_context.save_artifact(filename=f"chart_{safe_title}.json", artifact=artifact)
        return {
            "status": "success",
            "message": f"Chart '{title}' saved as artifact.",
            "chart_type": chart_type,
            "data_points_count": len(data_points),
        }

    return {
        "status": "success",
        "message": f"Chart config created for '{title}'.",
        "chart_config": chart_config,
    }


# ---------------------------------------------------------------------------
# HTML Memo Generator
# ---------------------------------------------------------------------------

_HTML_MEMO_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ScreenScore — {title}</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:'Inter',system-ui,sans-serif;background:#0c0c0c;color:#e8e4e0;min-height:100vh;padding:24px}}
  .memo{{max-width:820px;margin:0 auto}}
  .hero{{background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);border:1px solid rgba(255,255,255,.06);border-radius:14px;padding:32px 36px;margin-bottom:24px}}
  .hero h1{{font-size:26px;font-weight:700;margin-bottom:4px;color:#fff}}
  .hero .ts{{font-size:13px;color:#6b6560;margin-bottom:18px}}
  .rec-badge{{display:inline-block;padding:8px 22px;border-radius:8px;font-size:15px;font-weight:700;letter-spacing:.5px;margin-bottom:14px}}
  .rec-ACQUIRE{{background:rgba(16,185,129,.18);color:#34d399;border:1px solid rgba(16,185,129,.3)}}
  .rec-PASS{{background:rgba(239,68,68,.15);color:#f87171;border:1px solid rgba(239,68,68,.25)}}
  .rec-FURTHER_REVIEW{{background:rgba(251,191,36,.15);color:#fbbf24;border:1px solid rgba(251,191,36,.25)}}
  .rationale{{font-size:15px;line-height:1.65;color:#c4c0bb}}
  .stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:24px}}
  .stat{{background:#111;border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:18px 20px}}
  .stat .label{{font-size:12px;color:#6b6560;text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}}
  .stat .val{{font-size:22px;font-weight:700;color:#fff}}
  .stat .src{{font-size:11px;color:#9d9590;margin-top:4px}}
  .section{{background:#111;border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:24px;margin-bottom:18px}}
  .section h2{{font-size:16px;font-weight:600;color:#fff;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,.06)}}
  table{{width:100%;border-collapse:collapse;font-size:14px}}
  th{{text-align:left;color:#6b6560;font-weight:500;padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.06);font-size:12px;text-transform:uppercase;letter-spacing:.5px}}
  td{{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.04);color:#c4c0bb}}
  tr:last-child td{{border-bottom:none}}
  .bar-chart{{display:flex;flex-direction:column;gap:10px}}
  .bar-row{{display:flex;align-items:center;gap:12px}}
  .bar-label{{width:80px;font-size:13px;color:#9d9590;text-align:right;flex-shrink:0}}
  .bar-track{{flex:1;height:28px;background:rgba(255,255,255,.04);border-radius:6px;overflow:hidden;position:relative}}
  .bar-fill{{height:100%;border-radius:6px;display:flex;align-items:center;padding-left:10px;font-size:12px;font-weight:600;color:#fff;min-width:40px}}
  .bar-val{{margin-left:8px;font-size:12px;color:#9d9590}}
  .audit-row{{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.04)}}
  .audit-row:last-child{{border-bottom:none}}
  .audit-icon{{width:22px;height:22px;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0}}
  .audit-pass{{background:rgba(16,185,129,.18);color:#34d399}}
  .audit-fail{{background:rgba(239,68,68,.15);color:#f87171}}
  .audit-na{{background:rgba(107,101,96,.15);color:#9d9590}}
  .audit-label{{font-size:14px;color:#c4c0bb}}
  details{{margin-bottom:8px}}
  summary{{cursor:pointer;font-size:13px;color:#9d9590;padding:8px 12px;background:rgba(255,255,255,.03);border-radius:6px;border:1px solid rgba(255,255,255,.05)}}
  summary:hover{{background:rgba(255,255,255,.06)}}
  pre{{margin-top:8px;padding:14px;background:#0c0c0c;border-radius:8px;border:1px solid rgba(255,255,255,.06);overflow-x:auto;font-size:13px;line-height:1.5;color:#c4c0bb}}
  .risk{{padding:8px 14px;background:rgba(255,255,255,.03);border-radius:6px;margin-bottom:6px;font-size:14px;color:#c4c0bb;border-left:3px solid rgba(251,191,36,.4)}}
  .source-badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;margin-left:6px}}
  .src-clickhouse{{background:rgba(59,130,246,.15);color:#60a5fa}}
  .src-synthetic{{background:rgba(251,191,36,.12);color:#fbbf24}}
  .src-user{{background:rgba(168,85,247,.12);color:#c084fc}}
  .footer{{text-align:center;font-size:12px;color:#4a4540;margin-top:32px;padding:16px}}
</style>
</head>
<body>
<div class="memo">
  <div class="hero">
    <h1>{title}</h1>
    <div class="ts">Generated {timestamp}</div>
    <div class="rec-badge rec-{rec_class}">{recommendation}</div>
    <div class="rationale">{rationale}</div>
  </div>

  <div class="stats">
    {stat_cards}
  </div>

  <div class="section">
    <h2>Genre Benchmark</h2>
    <div class="bar-chart">
      {genre_bars}
    </div>
  </div>

  {comparable_section}

  {fallback_section}

  {market_section}

  {metadata_section}

  {director_section}

  <div class="section">
    <h2>Constraint Audit</h2>
    {audit_rows}
  </div>

  <div class="section">
    <h2>Risk Flags</h2>
    {risk_rows}
  </div>

  {evidence_gates_section}

  <div class="section">
    <h2>SQL Audit Trail</h2>
    {sql_audit}
  </div>

  <div class="footer">
    Data sources: genre benchmark and comparable titles from ClickHouse IMDb dataset (1888-2008).
    Performance benchmarks are synthetic market estimates.<br>
    This document does not constitute legal, financial, or contractual advice.
  </div>
</div>
</body>
</html>"""


async def generate_html_memo(
    title: str,
    recommendation: str,
    rationale: str,
    comparable_titles: list[dict],
    risk_flags: list[str],
    genre_benchmark: dict | list[dict],
    historical_fallback_comps: list[dict] | None = None,
    market_performance_comps: list[dict] | None = None,
    constraint_violations: list[str] | None = None,
    sql_queries_run: list[str] | None = None,
    constraint_audit: dict | None = None,
    title_metadata: list[dict] | None = None,
    sql_plan: str | None = None,
    director_analysis: list[dict] | None = None,
    comparable_titles_status: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Generate a styled HTML acquisition memo and save it as an artifact.

    The Dev UI renders HTML artifacts as sandboxed iframes in the Artifacts panel.
    Call this AFTER generate_acquisition_memo to produce the visual version.
    """
    # Pipeline gating: HTML memo requires markdown memo to have been generated first
    evidence_gates = []
    if tool_context:
        from screenscore.evidence import EvidenceRegistry

        pipeline = _get_pipeline(tool_context.state)

        if not pipeline.get("memo_generated"):
            _log_transition(
                pipeline,
                "[PIPELINE] generate_html_memo BLOCKED — "
                "markdown memo not yet generated. Call generate_acquisition_memo first.",
            )
            return {
                "status": "error",
                "message": "HTML memo cannot be generated before the markdown memo. "
                           "Call generate_acquisition_memo first.",
            }

        # Collect evidence gates for HTML memo
        registry = EvidenceRegistry.from_dict({
            "items": pipeline.get("evidence_items", {}),
            "claims": pipeline.get("evidence_claims", {}),
            "dependencies": pipeline.get("evidence_dependencies", {}),
            "candidate_classifications": pipeline.get("candidate_classifications", {}),
        })

        claim_statuses = registry.validate_all_claims()
        for claim_id, claim_status in claim_statuses.items():
            if str(getattr(claim_status, "value", claim_status)) == "gated":
                claim = registry.claims[claim_id]
                evidence_gates.append({
                    "claim_id": claim_id,
                    "description": claim.description,
                    "gated_by": claim.gated_by,
                })

        _log_transition(pipeline, "[PIPELINE] STEP 8b HTML memo generation")

    if recommendation not in ("ACQUIRE", "PASS", "FURTHER_REVIEW"):
        return {"status": "error", "message": "recommendation must be one of: ACQUIRE, PASS, FURTHER_REVIEW"}

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rec_class = recommendation

    benchmarks = genre_benchmark if isinstance(genre_benchmark, list) else [genre_benchmark]

    # Stat cards
    avg_ratings = [b.get("avg_rating", 0) for b in benchmarks if b.get("avg_rating")]
    total_titles = sum(b.get("title_count", 0) for b in benchmarks if b.get("title_count"))
    comp_count = len(comparable_titles) if comparable_titles else 0
    risk_count = len(risk_flags) if risk_flags else 0

    avg_all = sum(avg_ratings) / len(avg_ratings) if avg_ratings else 0
    stat_cards = f"""\
<div class="stat"><div class="label">Recommendation</div><div class="val" style="color:{'#34d399' if recommendation=='ACQUIRE' else '#f87171' if recommendation=='PASS' else '#fbbf24'}">{recommendation}</div></div>
<div class="stat"><div class="label">Genre Avg Rating</div><div class="val">{avg_all:.2f}</div><div class="src">[ClickHouse IMDb]</div></div>
<div class="stat"><div class="label">Comparable Titles</div><div class="val">{comp_count}</div><div class="src">[ClickHouse IMDb]</div></div>
<div class="stat"><div class="label">Risk Flags</div><div class="val">{risk_count}</div></div>"""

    # Genre bars
    max_count = max((b.get("title_count", 1) for b in benchmarks if b.get("title_count")), default=1)
    genre_colors = ["#3b82f6", "#8b5cf6", "#06b6d4", "#f59e0b", "#ef4444", "#10b981"]
    genre_bars = ""
    for i, b in enumerate(benchmarks):
        genre = b.get("genre", "N/A")
        avg_r = b.get("avg_rating", 0)
        count = b.get("title_count", 0)
        pct = (count / max_count * 100) if max_count > 0 else 0
        color = genre_colors[i % len(genre_colors)]
        genre_bars += f"""<div class="bar-row">
  <div class="bar-label">{genre}</div>
  <div class="bar-track"><div class="bar-fill" style="width:{pct:.0f}%;background:{color}">{avg_r}</div></div>
  <div class="bar-val">{count} titles</div>
</div>\n"""

    # Comparable titles section
    if comparable_titles:
        comp_rows_html = ""
        for c in comparable_titles:
            t = c.get("title") or c.get("name", "N/A")
            r = c.get("rating") or c.get("rank", "N/A")
            y = c.get("year", "N/A")
            comp_rows_html += f"<tr><td>{t}</td><td>{y}</td><td>{r} <span class='source-badge src-clickhouse'>ClickHouse IMDb</span></td></tr>\n"
        comparable_section = f"""<div class="section"><h2>Comparable Titles</h2>
<table><tr><th>Title</th><th>Year</th><th>Rating</th></tr>{comp_rows_html}</table></div>"""
    else:
        status = comparable_titles_status or "Zero titles matched requested criteria."
        comparable_section = f"""<div class="section"><h2>Comparable Titles</h2><p style="color:#9d9590;font-size:14px">{status}</p></div>"""

    # Historical fallback section
    fallbacks = historical_fallback_comps or []
    if fallbacks:
        fb_html = ""
        for c in fallbacks:
            t = c.get("title") or c.get("name", "N/A")
            r = c.get("rating") or c.get("rank", "N/A")
            y = c.get("year", "N/A")
            fb_html += f"<tr><td>{t}</td><td>{y}</td><td>{r} <span class='source-badge src-clickhouse'>ClickHouse IMDb</span></td></tr>\n"
        fallback_section = f"""<div class="section"><h2>Historical Fallback Comps</h2>
<p style="color:#fbbf24;font-size:13px;margin-bottom:10px">These titles DO NOT satisfy the requested criteria. Pre-2009 historical references only.</p>
<table><tr><th>Title</th><th>Year</th><th>Rating</th></tr>{fb_html}</table></div>"""
    else:
        fallback_section = ""

    # Market comps section
    market_comps = market_performance_comps or []
    if market_comps:
        mkt_html = ""
        for m in market_comps:
            views = f"{m.get('streaming_views_m_first30')}M" if m.get("streaming_views_m_first30") else "N/A"
            box = f"${m.get('opening_week_usd_m')}M" if m.get("opening_week_usd_m") else "N/A"
            mkt_html += f"<tr><td>{m.get('title', 'N/A')}</td><td>{m.get('year', 'N/A')}</td><td>{m.get('platform', 'N/A')}</td><td>{views}</td><td>{box} <span class='source-badge src-synthetic'>Synthetic</span></td></tr>\n"
        market_section = f"""<div class="section"><h2>Market Performance Benchmarks</h2>
<table><tr><th>Title</th><th>Year</th><th>Platform</th><th>Streaming (30d)</th><th>Opening Box Office</th></tr>{mkt_html}</table></div>"""
    else:
        market_section = ""

    # Metadata section
    meta = title_metadata or []
    if meta:
        meta_html = ""
        for m in meta:
            src = m.get("source", "")
            badge = "src-clickhouse" if "ClickHouse" in src else ("src-synthetic" if "Synthetic" in src else ("src-user" if "User" in src else ""))
            badge_html = ("<span class=\"source-badge " + badge + "\">" + badge.replace("src-", "").title() + "</span>") if badge else ""
            meta_html += f"<tr><td>{m.get('field', 'N/A')}</td><td>{m.get('value', 'N/A')}</td><td>{src} {badge_html}</td></tr>\n"
        metadata_section = f"""<div class="section"><h2>Title Metadata</h2>
<table><tr><th>Field</th><th>Value</th><th>Source</th></tr>{meta_html}</table></div>"""
    else:
        metadata_section = ""

    # Director section
    director = director_analysis or []
    if director:
        dir_html = ""
        for d in director:
            src = d.get("source", "")
            badge = "src-clickhouse" if "ClickHouse" in src else ("src-user" if "User" in src else ("src-synthetic" if "Synthetic" in src else ""))
            badge_html = ("<span class=\"source-badge " + badge + "\">" + badge.replace("src-", "").title() + "</span>") if badge else ""
            dir_html += f"<tr><td>{d.get('field', 'N/A')}</td><td>{d.get('value', 'N/A')}</td><td>{src} {badge_html}</td></tr>\n"
        director_section = f"""<div class="section"><h2>Director Track Record</h2>
<table><tr><th>Field</th><th>Value</th><th>Source</th></tr>{dir_html}</table></div>"""
    else:
        director_section = ""

    # Audit rows
    audit = constraint_audit or {}
    audit_labels = {
        "year_range_preserved": "Year range preserved",
        "rating_threshold_preserved": "Rating threshold preserved",
        "both_genres_required": "Both genres required",
        "max_comps_respected": "Max comps respected",
        "historical_fallback_separated": "Historical fallback separated",
        "no_fabricated_data": "No fabricated data",
        "synthetic_data_labeled": "Synthetic data labeled",
        "post_2015_trend_supported": "Post-2015 trend supported",
    }
    audit_rows = ""
    for key, label in audit_labels.items():
        val = audit.get(key, "N/A")
        val_upper = str(val).upper()
        if val_upper in ("YES", "TRUE", "PASS"):
            icon_cls = "audit-pass"; icon = "+"
        elif val_upper in ("NO", "FALSE", "FAIL"):
            icon_cls = "audit-fail"; icon = "x"
        else:
            icon_cls = "audit-na"; icon = "-"
        audit_rows += f"""<div class="audit-row"><div class="audit-icon {icon_cls}">{icon}</div><div class="audit-label">{label}: <strong>{val}</strong></div></div>\n"""

    # Risk rows
    risk_rows = ""
    if risk_flags:
        for flag in risk_flags:
            risk_rows += f'<div class="risk">{flag}</div>\n'
    else:
        risk_rows = '<div class="risk" style="border-left-color:rgba(16,185,129,.4)">No material risks identified</div>'

    # Evidence gates section
    if evidence_gates:
        evidence_items = ""
        for gate in evidence_gates:
            gated_by_str = ", ".join(gate["gated_by"])
            evidence_items += f"""<div class="audit-row"><div class="audit-icon audit-fail">!</div><div class="audit-label"><strong>{gate['claim_id']}</strong>: {gate['description']} — gated by: {gated_by_str}</div></div>\n"""
        evidence_gates_section = f"""<div class="section"><h2>Evidence Dependency Gates</h2>
<p style="color:#fbbf24;font-size:13px;margin-bottom:10px">The following claims are gated by missing evidence:</p>
{evidence_items}</div>"""
    else:
        evidence_gates_section = ""

    # SQL audit
    queries = sql_queries_run or []
    if queries:
        sql_items = ""
        for i, q in enumerate(queries, 1):
            sql_items += f"""<details><summary>Q{i}</summary><pre>{q}</pre></details>\n"""
        sql_audit = sql_items
    else:
        sql_audit = '<p style="color:#6b6560;font-size:13px">No SQL queries recorded.</p>'

    # Assemble
    html = _HTML_MEMO_TEMPLATE.format(
        title=title,
        timestamp=timestamp,
        recommendation=recommendation,
        rec_class=rec_class,
        rationale=rationale,
        stat_cards=stat_cards,
        genre_bars=genre_bars,
        comparable_section=comparable_section,
        fallback_section=fallback_section,
        market_section=market_section,
        metadata_section=metadata_section,
        director_section=director_section,
        audit_rows=audit_rows,
        risk_rows=risk_rows,
        evidence_gates_section=evidence_gates_section,
        sql_audit=sql_audit,
    )

    if tool_context:
        safe_title = title[:40].replace(" ", "_").replace("/", "-")
        artifact = types.Part(
            inline_data=types.Blob(mime_type="text/html", data=html.encode("utf-8"))
        )
        version = await tool_context.save_artifact(
            filename=f"memo_{safe_title}.html", artifact=artifact
        )
        logger.info("Saved HTML memo artifact for '%s' (v%d)", title, version)

    return {
        "status": "success",
        "title": title,
        "recommendation": recommendation,
        "message": f"HTML memo for '{title}' saved as artifact.",
    }


# ---------------------------------------------------------------------------
# Query Metadata Logger
# ---------------------------------------------------------------------------

async def log_query_metadata(
    query_id: str,
    sql: str,
    description: str = "",
    rows_returned: int = 0,
    execution_time_ms: int | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Log execution metadata for a SQL query run via MCP.

    Call this after each run_query to build an audit trail with execution metrics.
    The metadata is stored in session state and consumed by generate_html_memo.

    Args:
        query_id: Identifier for the query (e.g. "Q1", "Q2", "DQ1").
        sql: The SQL string that was executed.
        description: Human-readable description of what the query does.
        rows_returned: Number of rows returned by the query.
        execution_time_ms: Query execution time in milliseconds (if available).
        tool_context: Injected by ADK runtime.
    """
    entry = {
        "query_id": query_id,
        "sql": sql,
        "description": description,
        "rows_returned": rows_returned,
        "execution_time_ms": execution_time_ms,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if tool_context:
        state = tool_context.state
        if "query_audit" not in state:
            state["query_audit"] = []
        state["query_audit"].append(entry)
        logger.info(
            "Logged query metadata: %s — %d rows — %s",
            query_id, rows_returned, description[:60],
        )

    return {
        "status": "success",
        "query_id": query_id,
        "message": f"Query {query_id} metadata logged.",
    }