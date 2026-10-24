import pytest
from unittest.mock import patch, AsyncMock
from src.telegram.bot import TelegramAssistant
from config.settings import settings


@pytest.fixture(autouse=True)
def _owner_chat_id(monkeypatch):
    """Commands are only accepted from the configured owner chat (12345 in these tests)."""
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "12345")

@pytest.mark.asyncio
async def test_telegram_register_bot_commands():
    bot = TelegramAssistant()
    bot.token = "123456789:ABCDEF_mock_token_for_test"
    bot.base_url = f"https://api.telegram.org/bot{bot.token}"

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.json = lambda: {"ok": True, "result": True}
        mock_post.return_value = mock_resp

        success = await bot.register_bot_commands()
        assert success is True
        assert mock_post.call_count >= 2
        # Check that setMyCommands was called
        set_calls = [c for c in mock_post.call_args_list if "/setMyCommands" in c[0][0]]
        assert len(set_calls) >= 1
        payload = set_calls[-1][1]["json"]
        commands = [c["command"] for c in payload["commands"]]
        assert "portfolio" in commands
        assert "review" in commands
        assert "triggers" in commands
        assert "trigger" in commands
        assert "help" in commands

@pytest.mark.asyncio
async def test_telegram_handle_slash_command():
    bot = TelegramAssistant()
    bot.token = "123456789:ABCDEF_mock_token_for_test"
    bot.base_url = f"https://api.telegram.org/bot{bot.token}"

    replies = []
    async def mock_reply(chat_id, text):
        replies.append((chat_id, text))

    bot._reply_chat = mock_reply

    # Test sending "/" directly
    update_slash = {
        "update_id": 101,
        "message": {
            "chat": {"id": 12345},
            "text": "/"
        }
    }
    await bot._handle_update(update_slash)
    assert len(replies) == 1
    assert replies[0][0] == 12345
    assert "/portfolio" in replies[0][1]
    assert "/review" in replies[0][1]
    assert "/trigger" in replies[0][1]

    # Test sending "/help"
    update_help = {
        "update_id": 102,
        "message": {
            "chat": {"id": 12345},
            "text": "/help"
        }
    }
    await bot._handle_update(update_help)
    assert len(replies) == 2
    assert "/portfolio" in replies[1][1]

    # Test sending "/commands"
    update_commands = {
        "update_id": 103,
        "message": {
            "chat": {"id": 12345},
            "text": "/commands"
        }
    }
    await bot._handle_update(update_commands)
    assert len(replies) == 3
    assert "/portfolio" in replies[2][1]

@pytest.mark.asyncio
async def test_telegram_calibrate_with_trades():
    bot = TelegramAssistant()
    bot.token = "123456789:ABCDEF_mock_token_for_test"
    bot.base_url = f"https://api.telegram.org/bot{bot.token}"

    replies = []
    async def mock_reply(chat_id, text):
        replies.append((chat_id, text))
    bot._reply_chat = mock_reply

    # Case 1: calibrate with trades success
    mock_cal_res = {
        "success": True,
        "total_trades": 5,
        "win_rate_pct": 60.0,
        "consecutive_losses": 1,
        "calibrated_parameters": {
            "min_consensus_score": 76,
            "position_size_pct": 0.06,
            "atr_stop_multiplier": 2.1,
            "take_profit_rr": 2.2,
        },
        "adjustments": ["Normalized parameters for current regime"]
    }
    with patch("src.trading.learner.trade_learner.calibrate_internal_parameters", AsyncMock(return_value=mock_cal_res)):
        update = {"message": {"chat": {"id": 12345}, "text": "/calibrate"}}
        await bot._handle_update(update)
        assert len(replies) >= 2
        complete_reply = replies[-1][1]
        assert "CALIBRATION COMPLETE" in complete_reply
        assert "76/100" in complete_reply
        assert "6.0%" in complete_reply
        assert "2.1x" in complete_reply

    # Case 2: calibrate without closed trades
    replies.clear()
    mock_no_trades = {
        "success": False,
        "message": "No closed trades available for calibration."
    }
    with patch("src.trading.learner.trade_learner.calibrate_internal_parameters", AsyncMock(return_value=mock_no_trades)):
        update = {"message": {"chat": {"id": 12345}, "text": "/tune"}}
        await bot._handle_update(update)
        assert len(replies) >= 2
        assert "CALIBRATION NOTICE" in replies[-1][1]

@pytest.mark.asyncio
async def test_telegram_portfolio_command():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    mock_summary = {
        "total_value_eur": 10500.0,
        "cash_eur": 8000.0,
        "equity_eur": 2500.0,
        "unrealized_pnl_eur": 500.0,
        "total_return_pct": 5.0,
        "open_positions_count": 1
    }
    mock_positions = [
        {
            "ticker": "NVDA",
            "shares": 10.0,
            "current_price_native": 250.0,
            "currency": "USD",
            "unrealized_pnl_eur": 500.0,
            "unrealized_pnl_pct": 25.0
        }
    ]
    with patch("src.trading.paper.paper_broker.get_portfolio_summary", AsyncMock(return_value=mock_summary)), \
         patch("src.trading.paper.paper_broker.get_positions", AsyncMock(return_value=mock_positions)):
        update = {"message": {"chat": {"id": 12345}, "text": "/portfolio"}}
        await bot._handle_update(update)
        assert len(replies) == 1
        text = replies[0][1]
        assert "PORTFOLIO SUMMARY" in text
        assert "€10,500.00" in text
        assert "NVDA" in text
        assert "+€500.00" in text

@pytest.mark.asyncio
async def test_telegram_triggers_and_trigger_command():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    # 1. /triggers empty
    with patch("src.trading.paper.paper_broker.get_triggers", AsyncMock(return_value=[])):
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/triggers"}})
        assert "NO ACTIVE AI TRIGGERS" in replies[-1][1]

    # 2. /triggers with items (including None rationale)
    mock_triggers = [
        {
            "id": 1,
            "ticker": "ASML.AS",
            "trigger_type": "LIMIT_PULLBACK",
            "target_price": 750.0,
            "current_price": 770.0,
            "distance_pct": -2.6,
            "currency": "EUR",
            "allocation_eur": 500.0,
            "stop_loss": 720.0,
            "take_profit": 820.0,
            "ai_rationale": None,
            "status": "PENDING"
        }
    ]
    with patch("src.trading.paper.paper_broker.get_triggers", AsyncMock(return_value=mock_triggers)):
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/triggers"}})
        assert "ACTIVE AI PRICE TRIGGERS" in replies[-1][1]
        assert "ASML.AS" in replies[-1][1]

    # 3. /trigger $NVDA
    mock_stock = {"ticker": "NVDA", "name": "NVIDIA", "price": 120.0, "currency": "USD"}
    mock_summary = {"total_value_eur": 10000.0, "available_cash_eur": 5000.0}
    mock_report = {
        "action": "BUY",
        "consensus_score": 85,
        "entry_trigger_price": 120.0,
        "trigger_type": "LIMIT_PULLBACK",
        "trigger_rationale": "High AI score with favorable pullback",
        "risk": {"stop_loss_price": 115.0, "take_profit_price": 130.0, "recommended_size_pct": 0.08}
    }
    mock_trig_res = {"success": True, "trigger_id": 42, "message": "Trigger armed"}
    with patch("src.data.market.market_data_provider.get_stock_data", return_value=mock_stock), \
         patch("src.trading.paper.paper_broker.get_portfolio_summary", AsyncMock(return_value=mock_summary)), \
         patch("src.agents.committee.investment_committee.evaluate_ticker", AsyncMock(return_value=mock_report)), \
         patch("src.trading.paper.paper_broker.create_price_trigger", AsyncMock(return_value=mock_trig_res)) as mock_create:
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/trigger $NVDA"}})
        assert "AI PRICE TRIGGER ARMED #42" in replies[-1][1]
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["target_price"] < 120.0

@pytest.mark.asyncio
async def test_telegram_cancel_trigger():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    # Test missing argument
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/cancel_trigger"}})
    assert "Usage: <code>/cancel_trigger ID_OR_TICKER</code>" in replies[-1][1]

    # Test with # prefix
    with patch("src.trading.paper.paper_broker.cancel_price_trigger", AsyncMock(return_value={"success": True, "message": "Trigger #5 cancelled."})) as mock_cancel:
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/cancel_trigger #5"}})
        mock_cancel.assert_called_with(5)
        assert "Trigger #5 cancelled" in replies[-1][1]

    # Test with ticker symbol (matching pending trigger)
    mock_pending = [
        {"id": 7, "ticker": "NVDA", "status": "PENDING"},
        {"id": 8, "ticker": "AAPL", "status": "PENDING"}
    ]
    with patch("src.trading.paper.paper_broker.get_triggers", AsyncMock(return_value=mock_pending)), \
         patch("src.trading.paper.paper_broker.cancel_price_trigger", AsyncMock(return_value={"success": True})) as mock_cancel:
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/cancel NVDA"}})
        mock_cancel.assert_called_once_with(7)
        assert "Cancelled pending trigger(s) #7 for <b>NVDA</b>" in replies[-1][1]

    # Test with ticker symbol (none matching)
    with patch("src.trading.paper.paper_broker.get_triggers", AsyncMock(return_value=mock_pending)):
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/cancel_trigger TSLA"}})
        assert "No active pending trigger found for <b>TSLA</b>" in replies[-1][1]

@pytest.mark.asyncio
async def test_telegram_stop_resume_status():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    # Test /stop
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/stop"}})
    assert "Automated buying has been PAUSED" in replies[-1][1]

    # Test /status
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/status"}})
    assert "PAUSED" in replies[-1][1]
    assert "European Markets" in replies[-1][1]

    # Test /resume
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/resume"}})
    assert "Automated buying has been ENABLED" in replies[-1][1]

    # Test /status again
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/status"}})
    assert "ENABLED" in replies[-1][1]

@pytest.mark.asyncio
async def test_telegram_watchlist_crud():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    # Missing arg usage checks
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/watchlist add"}})
    assert "Usage: <code>/watchlist add TICKER</code>" in replies[-1][1]

    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/watchlist del"}})
    assert "Usage: <code>/watchlist del TICKER</code>" in replies[-1][1]

    # /watchlist add TSLA
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/watchlist add $TSLA"}})
    assert "<b>TSLA</b>" in replies[-1][1] and "added to active radar" in replies[-1][1]

    # /watchlist +NVDA shortcut
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/watchlist +NVDA"}})
    assert "<b>NVDA</b>" in replies[-1][1] and "added to active radar" in replies[-1][1]

    # /watchlist list
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/watchlist"}})
    assert "TSLA" in replies[-1][1]

    # /watchlist del TSLA
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/watchlist del TSLA"}})
    assert "<b>TSLA</b> removed from active watchlist" in replies[-1][1]

    # /watchlist -NVDA shortcut
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/watchlist -NVDA"}})
    assert "<b>NVDA</b> removed from active watchlist" in replies[-1][1]

@pytest.mark.asyncio
async def test_telegram_unknown_command():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/nonexistent_cmd"}})
    assert len(replies) == 1
    assert "Unknown command" in replies[0][1]
    assert "/help" in replies[0][1]

@pytest.mark.asyncio
async def test_telegram_reply_chunking_and_fallback():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    # Test chunking of huge text
    huge_text = "\n".join([f"Line {i}: test message content" for i in range(300)])
    chunks = bot._chunk_text(huge_text, max_len=1000)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 1000

    # Test _reply_chat with 400 bad request triggering fallback plain text
    with patch("httpx.AsyncClient.post") as mock_post:
        resp_400 = AsyncMock()
        resp_400.status_code = 400
        resp_400.text = "Bad Request: can't parse entities"

        resp_200 = AsyncMock()
        resp_200.status_code = 200
        resp_200.text = "OK"

        mock_post.side_effect = [resp_400, resp_200]

        res = await bot._reply_chat(12345, "<b>Invalid HTML < tag")
        assert res is True
        assert mock_post.call_count == 2

@pytest.mark.asyncio
async def test_telegram_analyze_command():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    mock_stock = {"ticker": "AAPL", "name": "Apple Inc", "price": 220.0, "currency": "USD"}
    mock_summary = {"total_value_eur": 10000.0, "available_cash_eur": 5000.0}
    mock_report = {
        "action": "BUY",
        "consensus_score": 75,
        "summary": "Solid fundamentals with tech support",
        "entry_trigger_price": 215.0,
        "trigger_type": "LIMIT_PULLBACK",
        "technical": {"bias": "BULLISH", "analysis_summary": "Above 50 SMA"},
        "fundamental": {"valuation": "FAIR"},
        "risk": {"stop_loss_price": 205.0, "take_profit_price": 240.0}
    }

    with patch("src.data.market.market_data_provider.get_stock_data", return_value=mock_stock), \
         patch("src.trading.paper.paper_broker.get_portfolio_summary", AsyncMock(return_value=mock_summary)), \
         patch("src.agents.committee.investment_committee.evaluate_ticker", AsyncMock(return_value=mock_report)):
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/analyze $AAPL"}})
        assert len(replies) >= 2
        verdict = replies[-1][1]
        assert "COMMITTEE VERDICT: BUY" in verdict
        assert "AAPL" in verdict
        assert "Above 50 SMA" in verdict

@pytest.mark.asyncio
async def test_telegram_review_and_compare_commands():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    # 1. /review US
    with patch("src.scheduler.jobs.trading_scheduler.run_market_session", AsyncMock(return_value=None)) as mock_run:
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/review US"}})
        assert "US Opening Market Review" in replies[0][1]
        mock_run.assert_called_once_with(market="US", session="OPEN", target_chat_id=12345)

    # 2. /wrap US (market close session)
    replies.clear()
    with patch("src.scheduler.jobs.trading_scheduler.run_market_session", AsyncMock(return_value=None)) as mock_run_close:
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/wrap US"}})
        assert "US Closing Wrap-Up" in replies[0][1]
        mock_run_close.assert_called_once_with(market="US", session="CLOSE", target_chat_id=12345)

    # 3. /compare
    replies.clear()
    mock_comp = {
        "has_history": True,
        "text_summary": "PAST-WEEK REVIEWS & PICK PERFORMANCE:\n• NVDA: +12.5% [IN PROFIT]"
    }
    with patch("src.data.review_tracker.review_tracker.compare_past_week_reviews", AsyncMock(return_value=mock_comp)):
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/compare"}})
        assert len(replies) >= 2
        assert "NVDA: +12.5%" in replies[-1][1]

@pytest.mark.asyncio
async def test_telegram_learning_command():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    mock_insights = {
        "total_closed_trades": 12,
        "wins": 8,
        "losses": 4,
        "win_rate_pct": 66.7,
        "total_realized_pnl_eur": 420.50,
        "active_parameters": {
            "min_consensus_score": 72,
            "position_size_pct": 0.08,
            "atr_stop_multiplier": 1.9,
            "take_profit_rr": 2.3
        },
        "recent_lessons": [
            {
                "ticker": "MSFT",
                "outcome": "WIN",
                "realized_pnl_pct": 5.2,
                "realized_pnl_eur": 85.0,
                "exit_reason": "Take Profit Target",
                "holding_period_hours": 18.5,
                "what_went_wrong": "Nothing",
                "lesson": "Trailing stop locked in gains effectively."
            }
        ]
    }
    with patch("src.trading.learner.trade_learner.get_learning_insights", AsyncMock(return_value=mock_insights)):
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/learning"}})
        assert len(replies) == 1
        text = replies[0][1]
        assert "AI TRADER POST-MORTEMS" in text
        assert "66.7%" in text
        assert "€420.50" in text
        assert "MSFT" in text

@pytest.mark.asyncio
async def test_telegram_buy_command():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    # 1. Missing args
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/buy"}})
    assert "Usage: <code>/buy TICKER [AMOUNT_EUR] [force]</code>" in replies[-1][1]

    # 2. Market closed check
    with patch("src.trading.risk.check_market_hours", return_value=(False, "US Market is closed.")):
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/buy NVDA"}})
        assert "Market Closed" in replies[-1][1]
        assert "/trigger NVDA" in replies[-1][1]

    # 3. Successful buy
    replies.clear()
    mock_stock = {"ticker": "NVDA", "name": "NVIDIA Corp", "price": 120.0, "currency": "USD"}
    mock_summary = {"total_value_eur": 10000.0, "available_cash_eur": 5000.0}
    mock_report = {
        "action": "BUY",
        "consensus_score": 85,
        "risk": {"stop_loss_price": 110.0, "take_profit_price": 140.0, "recommended_size_pct": 0.08}
    }
    mock_buy_res = {
        "success": True,
        "shares": 4,
        "price_native": 120.0,
        "price_eur": 110.0,
        "total_eur": 440.0,
        "stop_loss": 110.0,
        "take_profit": 140.0
    }
    with patch("src.trading.risk.check_market_hours", return_value=(True, "Market Open")), \
         patch("src.data.market.market_data_provider.get_stock_data", return_value=mock_stock), \
         patch("src.trading.paper.paper_broker.get_portfolio_summary", AsyncMock(return_value=mock_summary)), \
         patch("src.agents.committee.investment_committee.evaluate_ticker", AsyncMock(return_value=mock_report)), \
         patch("src.trading.paper.paper_broker.execute_buy", AsyncMock(return_value=mock_buy_res)):
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/buy $NVDA 500"}})
        assert len(replies) >= 2
        assert "MANUAL BUY ORDER EXECUTED" in replies[-1][1]
        assert "NVDA" in replies[-1][1]
        assert "€440.00" in replies[-1][1]

@pytest.mark.asyncio
async def test_telegram_sell_command():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    # 1. Missing args
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/sell"}})
    assert "Usage: <code>/sell TICKER [REASON]</code>" in replies[-1][1]

    # 2. Market closed check
    with patch("src.trading.risk.check_market_hours", return_value=(False, "Market is closed.")):
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/sell NVDA"}})
        assert "Market Closed" in replies[-1][1]

    # 3. Successful sell
    replies.clear()
    mock_sell_res = {
        "success": True,
        "exit_price_native": 130.0,
        "shares": 5,
        "proceeds_eur": 600.0,
        "realized_pnl_eur": 50.0,
        "realized_pnl_pct": 9.1
    }
    with patch("src.trading.risk.check_market_hours", return_value=(True, "Market Open")), \
         patch("src.trading.paper.paper_broker.execute_sell", AsyncMock(return_value=mock_sell_res)):
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/close NVDA Take Profit"}})
        assert len(replies) == 1
        assert "POSITION CLOSED" in replies[0][1]
        assert "+€50.00" in replies[0][1]
        assert "Take Profit" in replies[0][1]

@pytest.mark.asyncio
async def test_telegram_trades_command():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/trades"}})
    assert len(replies) == 1
    assert "TRADE" in replies[0][1]

@pytest.mark.asyncio
async def test_telegram_status_aliases():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    for alias in ("/health", "/ping", "/stats", "/info"):
        replies.clear()
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": alias}})
        assert len(replies) == 1
        assert "SYSTEM OPERATIONAL" in replies[0][1]
        assert "European Markets" in replies[0][1]

@pytest.mark.asyncio
async def test_telegram_calibrate_string_types():
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"

    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append((cid, txt)))

    mock_cal_res = {
        "success": True,
        "total_trades": 8,
        "win_rate_pct": 62.5,
        "consecutive_losses": 0,
        "calibrated_parameters": {
            "min_consensus_score": "75",
            "position_size_pct": "0.08",
            "atr_stop_multiplier": "1.8",
            "take_profit_rr": "2.2",
        },
        "adjustments": ["Calibrated string parameters"]
    }
    with patch("src.trading.learner.trade_learner.calibrate_internal_parameters", AsyncMock(return_value=mock_cal_res)):
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/calibrate"}})
        assert len(replies) >= 2
        assert "CALIBRATION COMPLETE" in replies[-1][1]
        assert "8.0%" in replies[-1][1]


def _capture_bot():
    bot = TelegramAssistant()
    replies = []

    async def mock_reply(chat_id, text):
        replies.append((chat_id, text))

    bot._reply_chat = mock_reply
    return bot, replies


@pytest.mark.asyncio
async def test_telegram_ignores_unauthorized_chat():
    bot, replies = _capture_bot()
    await bot._handle_update({"message": {"chat": {"id": 999}, "text": "/killswitch"}})
    await bot._handle_update({"message": {"chat": {"id": 999}, "text": "/help"}})
    assert replies == []


@pytest.mark.asyncio
async def test_telegram_unconfigured_chat_id_not_adopted(monkeypatch, caplog):
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", None)
    bot, replies = _capture_bot()
    bot.chat_id = None
    with caplog.at_level("WARNING"):
        await bot._handle_update({"message": {"chat": {"id": 777}, "text": "/help"}})
    assert replies == []
    assert bot.chat_id is None
    assert "777" in caplog.text


@pytest.mark.asyncio
async def test_telegram_ignores_edited_message():
    bot, replies = _capture_bot()
    await bot._handle_update({"edited_message": {"chat": {"id": 12345}, "text": "/help"}})
    assert replies == []


@pytest.mark.asyncio
async def test_telegram_requires_leading_slash():
    bot, replies = _capture_bot()
    before = settings.KILL_SWITCH_ACTIVE
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "halt"}})
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "stop"}})
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "hi"}})
    assert replies == []
    assert settings.KILL_SWITCH_ACTIVE == before


@pytest.mark.asyncio
async def test_telegram_ignores_stale_messages():
    import time
    bot, replies = _capture_bot()
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/help", "date": int(time.time()) - 600}})
    assert replies == []
    await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/help", "date": int(time.time()) - 5}})
    assert len(replies) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["abc", "0", "-5", "nan", "inf"])
async def test_telegram_buy_invalid_amount_rejected(amount):
    bot, replies = _capture_bot()
    with patch("src.trading.risk.check_market_hours", return_value=(True, "Open")), \
         patch("src.telegram.bot.market_data_provider.get_stock_data") as mock_data:
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": f"/buy NVDA {amount}"}})
        mock_data.assert_not_called()
    assert len(replies) == 1
    assert "Invalid amount" in replies[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("price", ["abc", "0", "-5", "nan"])
async def test_telegram_trigger_invalid_price_rejected(price):
    bot, replies = _capture_bot()
    with patch("src.telegram.bot.market_data_provider.get_stock_data") as mock_data:
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": f"/trigger NVDA {price}"}})
        mock_data.assert_not_called()
    assert len(replies) == 1
    assert "Invalid price" in replies[0][1]
