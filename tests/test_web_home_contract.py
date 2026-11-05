import re
from pathlib import Path

from fastapi.testclient import TestClient

from src.web.app import app

HOME_IDS = (
    "brokerDisconnectBanner bannerBrokerName brokerDisconnectDetail brokerConnectedStrip "
    "stripBrokerName stripBrokerEndpoint stripBrokerLatency stripBrokerAccount metricTotalVal "
    "metricReturnBadge allocationBar metricEquity metricTotalCash allocationLegend "
    "mobileAutoBuyPill mobileAutoBuyText mobileAutoBuyDot killSwitchBtn mobileMarketStatusPill "
    "mobileMarketDot mobileMarketStatusText capitalLimitBadge metricCash metricCashSub "
    "metricUnrealizedPnl metricPosCount metricUnrealizedPnlSub pendingTriggersSummary "
    "pendingTriggersSummaryText pendingTriggersSummarySub positionsTitle positionsCount "
    "positionsContainer allocationChart holdingsSection holdingsTitle holdingsCount holdingsContainer metricMine"
).split()

APP_JS = Path(__file__).resolve().parent.parent / "src/web/static/app.js"


def _html():
    # No `with`: the lifespan (scheduler, Telegram, broker) is not started
    res = TestClient(app, client=("192.168.1.20", 5000)).get("/")
    assert res.status_code == 200
    return res.text


def test_home_ids_present():
    html = _html()
    missing = [i for i in HOME_IDS if f'id="{i}"' not in html]
    assert not missing, missing


def test_kill_switch_button_hooks():
    m = re.search(r'(<button\b[^>]*id="killSwitchBtn"[^>]*>)(.*?)</button>', _html(), re.S)
    assert m is not None, "#killSwitchBtn button not found"
    assert 'onclick="toggleKillSwitch()"' in m.group(1)
    assert re.search(r'class="(?:[^"]*\s)?kill-switch-label(?:\s[^"]*)?"', m.group(2))


def test_banner_before_total_value():
    html = _html()
    assert html.index('id="brokerDisconnectBanner"') < html.index('id="metricTotalVal"')


def test_app_js_position_actions():
    js = APP_JS.read_text(encoding="utf-8")
    for action in ("toggle-position", "close-position"):
        assert re.search(rf'<[^>]*\bdata-action="{action}"[^>]*\bdata-ticker=', js), action


def test_app_js_owner_actions():
    js = APP_JS.read_text(encoding="utf-8")
    assert "set-owner" in js
    assert "/api/holdings" in js
