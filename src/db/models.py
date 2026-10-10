import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, JSON
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class WatchlistItem(Base):
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=True)
    sector = Column(String(50), nullable=True)
    country = Column(String(10), nullable=True)
    is_active = Column(Boolean, default=True)
    added_at = Column(DateTime, default=datetime.datetime.utcnow)

class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, index=True)
    name = Column(String(100), nullable=True)
    currency = Column(String(10), default="USD")
    shares = Column(Float, nullable=False)
    
    # Native currency prices
    entry_price_native = Column(Float, nullable=False)
    current_price_native = Column(Float, nullable=False)
    stop_loss_native = Column(Float, nullable=True)
    take_profit_native = Column(Float, nullable=True)
    highest_price_native = Column(Float, nullable=False)  # For trailing stops
    
    # EUR converted values
    entry_price_eur = Column(Float, nullable=False)
    current_price_eur = Column(Float, nullable=False)
    cost_basis_eur = Column(Float, nullable=False)
    unrealized_pnl_eur = Column(Float, default=0.0)
    unrealized_pnl_pct = Column(Float, default=0.0)
    
    status = Column(String(20), default="OPEN")  # OPEN, CLOSED
    is_live = Column(Boolean, default=False)
    broker_order_id = Column(String(100), nullable=True)
    opened_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, index=True)
    name = Column(String(100), nullable=True)
    order_type = Column(String(10), nullable=False)  # BUY, SELL
    shares = Column(Float, nullable=False)
    
    currency = Column(String(10), default="USD")
    price_native = Column(Float, nullable=False)
    price_eur = Column(Float, nullable=False)
    total_eur = Column(Float, nullable=False)
    
    realized_pnl_eur = Column(Float, nullable=True)  # Only for SELL
    realized_pnl_pct = Column(Float, nullable=True)
    
    rationale = Column(Text, nullable=True)
    consensus_score = Column(Integer, nullable=True)
    is_live = Column(Boolean, default=False)
    broker_order_id = Column(String(100), nullable=True)
    executed_at = Column(DateTime, default=datetime.datetime.utcnow)

class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    cash_eur = Column(Float, nullable=False)
    equity_eur = Column(Float, nullable=False)
    total_value_eur = Column(Float, nullable=False)
    daily_pnl_eur = Column(Float, default=0.0)
    daily_pnl_pct = Column(Float, default=0.0)
    open_positions_count = Column(Integer, default=0)
    is_live = Column(Boolean, default=False)

class AnalysisReport(Base):
    __tablename__ = "analysis_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    
    action = Column(String(20), nullable=False)  # STRONG_BUY, BUY, HOLD, SELL
    consensus_score = Column(Integer, nullable=False)  # 0 to 100
    target_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    
    summary = Column(Text, nullable=True)
    technical_summary = Column(Text, nullable=True)
    fundamental_summary = Column(Text, nullable=True)
    sentiment_summary = Column(Text, nullable=True)
    risk_summary = Column(Text, nullable=True)
    debate_transcript = Column(Text, nullable=True)
    
    raw_signals = Column(JSON, nullable=True)
    executed = Column(Boolean, default=False)

class MarketDigest(Base):
    __tablename__ = "market_digests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    market = Column(String(10), nullable=False)  # EU, US
    session = Column(String(10), nullable=False)  # OPEN, CLOSE
    headline = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    top_picks = Column(JSON, nullable=True)
    portfolio_state = Column(JSON, nullable=True)
    review_text = Column(Text, nullable=True)

class SystemConfig(Base):
    __tablename__ = "system_configs"

    key = Column(String(50), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

class OrderTrigger(Base):
    __tablename__ = "order_triggers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, index=True)
    name = Column(String(100), nullable=True)
    currency = Column(String(10), default="USD")
    trigger_type = Column(String(20), default="LIMIT_PULLBACK")  # LIMIT_PULLBACK, BREAKOUT_BUY
    target_price = Column(Float, nullable=False)  # Trigger threshold price in native currency
    current_price_at_creation = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    allocation_eur = Column(Float, nullable=False)
    consensus_score = Column(Integer, nullable=True)
    ai_rationale = Column(Text, nullable=True)
    status = Column(String(20), default="PENDING", index=True)  # PENDING, TRIGGERED, CANCELLED, EXPIRED
    is_live = Column(Boolean, default=False)
    broker_order_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    triggered_at = Column(DateTime, nullable=True)
    executed_trade_id = Column(Integer, nullable=True)

class TradeLesson(Base):
    __tablename__ = "trade_lessons"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(Integer, nullable=False, index=True)
    ticker = Column(String(20), nullable=False, index=True)
    outcome = Column(String(20), nullable=False)  # WIN, LOSS, BREAKEVEN
    realized_pnl_eur = Column(Float, default=0.0)
    realized_pnl_pct = Column(Float, default=0.0)
    exit_reason = Column(String(255), nullable=True)
    holding_period_hours = Column(Float, default=0.0)
    what_went_well = Column(Text, nullable=True)
    what_went_wrong = Column(Text, nullable=True)
    lesson = Column(Text, nullable=False)
    parameter_adjustments = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
