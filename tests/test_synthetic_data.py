"""Tests for the synthetic data module."""
from screenscore.synthetic_data import _PERFORMANCE_DATA


def test_performance_data_has_expected_titles():
    assert "oppenheimer" in _PERFORMANCE_DATA
    assert "anora" in _PERFORMANCE_DATA
    assert "everything everywhere all at once" in _PERFORMANCE_DATA
    # 2024–2025 titles
    assert "dune part two" in _PERFORMANCE_DATA
    assert "wicked" in _PERFORMANCE_DATA
    assert "sinners" in _PERFORMANCE_DATA


def test_performance_data_is_separate_module():
    """Synthetic data lives in its own module for clear separation."""
    import screenscore.synthetic_data
    assert screenscore.synthetic_data.__name__ == "screenscore.synthetic_data"


def test_performance_data_structure():
    """Every entry must have the required base fields with correct types."""
    for title, data in _PERFORMANCE_DATA.items():
        assert "year" in data, f"{title}: missing 'year'"
        assert "genre" in data, f"{title}: missing 'genre'"
        assert "platform" in data, f"{title}: missing 'platform'"
        assert isinstance(data["year"], int), f"{title}: year must be int"


def test_performance_data_has_rich_fields():
    """All entries should have the enriched fields added in v2."""
    for title, data in _PERFORMANCE_DATA.items():
        assert "director" in data, f"{title}: missing 'director'"
        assert "cast" in data, f"{title}: missing 'cast'"
        assert "imdb_rating" in data, f"{title}: missing 'imdb_rating'"
        assert "box_office_total_usd_m" in data, f"{title}: missing 'box_office_total_usd_m'"
        assert "budget_usd_m" in data, f"{title}: missing 'budget_usd_m'"


def test_performance_data_covers_recent_years():
    years = {v["year"] for v in _PERFORMANCE_DATA.values()}
    assert 2021 in years
    assert 2022 in years
    assert 2023 in years
    assert 2024 in years
    assert 2025 in years


def test_oppenheimer_has_complete_data():
    """Oppenheimer should have full data to enable ACQUIRE decision."""
    opp = _PERFORMANCE_DATA["oppenheimer"]
    assert opp["imdb_rating"] == 8.3
    assert opp["box_office_total_usd_m"] == 952.0
    assert opp["director"] == "Christopher Nolan"
    assert "7 Oscars" in opp["awards"]