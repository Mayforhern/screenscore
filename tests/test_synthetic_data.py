"""Tests for the synthetic data module."""
from screenscore.synthetic_data import _PERFORMANCE_DATA


def test_performance_data_has_expected_titles():
    assert "oppenheimer" in _PERFORMANCE_DATA
    assert "anora" in _PERFORMANCE_DATA
    assert "everything everywhere all at once" in _PERFORMANCE_DATA


def test_performance_data_is_separate_module():
    """Synthetic data lives in its own module for clear separation."""
    import screenscore.synthetic_data
    assert screenscore.synthetic_data.__name__ == "screenscore.synthetic_data"


def test_performance_data_structure():
    for title, data in _PERFORMANCE_DATA.items():
        assert "year" in data
        assert "genre" in data
        assert "platform" in data
        assert isinstance(data["year"], int)


def test_performance_data_covers_recent_years():
    years = {v["year"] for v in _PERFORMANCE_DATA.values()}
    assert 2022 in years
    assert 2023 in years
    assert 2024 in years