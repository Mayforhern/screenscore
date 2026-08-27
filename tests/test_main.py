"""Tests for main.py and app startup."""
import pytest
from pathlib import Path


def test_main_imports():
    from main import adk_app
    assert adk_app is not None


def test_main_has_landing_routes():
    from main import adk_app
    routes = [r.path for r in adk_app.router.routes if hasattr(r, 'path')]
    assert "/" in routes
    assert "/home" in routes


def test_main_does_not_redirect_to_dev_ui():
    """The ADK redirect from / → /dev-ui/ should be removed."""
    from main import adk_app
    routes = [r.path for r in adk_app.router.routes if hasattr(r, 'path')]
    for r in routes:
        assert r != "/dev-ui/"


def test_index_html_exists():
    index_path = Path(__file__).parent.parent / "screenscore" / "templates" / "index.html"
    assert index_path.exists()
    content = index_path.read_text()
    assert "ScreenScore" in content or "screenscore" in content.lower()


def test_main_imports_logging():
    from main import logger
    assert logger is not None
    assert logger.name == "__main__" or logger.name == "main"