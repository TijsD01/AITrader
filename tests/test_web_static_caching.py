import re

from fastapi.testclient import TestClient

from src.web.app import app


def _client():
    # No `with`: the lifespan (scheduler, Telegram, broker) is not started
    return TestClient(app, client=("192.168.1.20", 5000))


def test_index_links_versioned_assets_and_is_revalidated():
    res = _client().get("/")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"
    assert re.search(r'href="/static/style\.css\?v=[0-9a-f]{10}"', res.text)
    assert re.search(r'src="/static/app\.js\?v=[0-9a-f]{10}"', res.text)


def test_static_files_must_be_revalidated():
    c = _client()
    url = re.search(r'href="(/static/style\.css\?v=[0-9a-f]+)"', c.get("/").text).group(1)
    res = c.get(url)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"
    assert "--bg:" in res.text
