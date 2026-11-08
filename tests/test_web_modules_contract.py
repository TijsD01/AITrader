import re
import shutil
import subprocess
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from src.web.app import app

MODULES = ("screener", "debate", "shadow", "memory")


def _client():
    # No `with`: the lifespan (scheduler, Telegram, broker) is not started
    return TestClient(app, client=("192.168.1.20", 5000))


def _html():
    res = _client().get("/")
    assert res.status_code == 200
    return res.text


def test_features_js_versioned_and_before_app_js():
    html = _html()
    f = re.search(r'src="/static/features\.js\?v=[0-9a-f]{10}"', html)
    a = re.search(r'src="/static/app\.js\?v=[0-9a-f]{10}"', html)
    assert f and a and f.start() < a.start()


def test_every_module_element_is_tagged():
    html = _html()
    for m in MODULES:
        assert re.search(rf'<section\b[^>]*id="tab-{m}"[^>]*data-module="{m}"', html), m
        assert len(re.findall(rf'data-tab="{m}"[^>]*data-module="{m}"|data-module="{m}"[^>]*data-tab="{m}"', html)) == 2, m


def test_core_tabs_are_not_modules():
    html = _html()
    for t in ("portfolio", "triggers", "advice", "history", "settings"):
        assert not re.search(rf'data-tab="{t}"[^>]*data-module=', html), t


def test_settings_has_modules_group():
    html = _html()
    assert 'data-fold="settings-modules"' in html
    assert 'id="moduleToggleList"' in html
    assert html.index('data-fold="settings-modules"') < html.index('data-fold="settings-trading"')


def test_features_js_is_served():
    assert _client().get("/static/features.js").status_code == 200


def test_features_js_unit_tests():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    root = Path(__file__).resolve().parent.parent
    res = subprocess.run(
        ["node", "--test", "tests/js/features.test.mjs"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_theme_follows_system():
    html = _html()
    css = _client().get("/static/style.css").text
    assert '<meta name="color-scheme" content="light dark">' in html
    assert "@media (prefers-color-scheme: dark)" in css
    assert "rgb(var(--rgb-" in html  # Tailwind colors read CSS vars
