"""Histórico de longo prazo: SQLite por padrão e PostgreSQL no Docker."""
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, Integer, MetaData, String, Table, create_engine, insert,
    select,
)
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
trades = Table(
    "trades", metadata,
    Column("trade_id", String(80), primary_key=True),
    Column("environment", String(20), nullable=False, index=True),
    Column("symbol", String(30), nullable=False, index=True),
    Column("strategy", String(20), nullable=False),
    Column("timeframe", String(10), nullable=False),
    Column("opened_at", DateTime(timezone=True), nullable=False),
    Column("closed_at", DateTime(timezone=True), nullable=False, index=True),
    Column("quantity", Float, nullable=False),
    Column("entry_price", Float, nullable=False),
    Column("exit_price", Float, nullable=False),
    Column("cost", Float, nullable=False),
    Column("proceeds", Float, nullable=False),
    Column("sold_value", Float, nullable=False),
    Column("fees", Float, nullable=False),
    Column("pnl", Float, nullable=False),
    Column("pnl_pct", Float, nullable=False),
    Column("exit_reason", String(60), nullable=False),
    Column("params", JSON, nullable=False),
)
validations = Table(
    "validation_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("symbols", String(300), nullable=False),
    Column("strategy", String(20), nullable=False),
    Column("timeframe", String(10), nullable=False),
    Column("approved", Boolean, nullable=False),
    Column("params", JSON, nullable=False),
    Column("pairs", JSON, nullable=False),
)
COLUMNS = [column.name for column in trades.columns]


def database_url():
    return os.environ.get("DATABASE_URL", "sqlite:///trades.db")


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc)


class History:
    def __init__(self, url=None):
        self.engine = create_engine(url or database_url(), pool_pre_ping=True)
        metadata.create_all(self.engine)

    def add_trade(self, record):
        """Repetir o mesmo trade não duplica: a chave é o trade_id."""
        values = dict(record)
        values["opened_at"] = utc(values["opened_at"])
        values["closed_at"] = utc(values["closed_at"])
        try:
            with self.engine.begin() as connection:
                connection.execute(insert(trades).values(**values))
        except IntegrityError:
            pass

    def add_validation(self, symbols, strategy, timeframe, approved, params, pairs):
        with self.engine.begin() as connection:
            connection.execute(insert(validations).values(
                created_at=datetime.now(timezone.utc), symbols=symbols, strategy=strategy,
                timeframe=timeframe, approved=approved, params=params, pairs=pairs,
            ))

    def validation_count(self):
        with self.engine.connect() as connection:
            return len(connection.execute(select(validations.c.id)).fetchall())

    def frame(self, symbol=None, days=None):
        query = select(trades).order_by(trades.c.closed_at)
        if symbol:
            query = query.where(trades.c.symbol == symbol)
        if days:
            query = query.where(trades.c.closed_at >= datetime.now(timezone.utc) - timedelta(days=days))
        with self.engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        frame = pd.DataFrame([dict(row) for row in rows], columns=COLUMNS)
        if not frame.empty:
            frame["opened_at"] = pd.to_datetime(frame["opened_at"], utc=True)
            frame["closed_at"] = pd.to_datetime(frame["closed_at"], utc=True)
        return frame

    def symbols(self):
        with self.engine.connect() as connection:
            rows = connection.execute(select(trades.c.symbol).distinct()).fetchall()
        return sorted(row[0] for row in rows)


def analyze(frame):
    """Calcula as métricas sobre operações fechadas, já com taxas em USDT."""
    empty = {"count": 0, "curve": [], "bySymbol": [], "monthly": [], "trades": []}
    if frame.empty:
        return empty
    wins = frame[frame["pnl"] > 0]
    losses = frame[frame["pnl"] <= 0]
    cumulative = frame["pnl"].cumsum()
    drawdown = (cumulative - cumulative.cummax().clip(lower=0)).min()
    gross_loss = -losses["pnl"].sum()
    by_symbol = [
        {
            "symbol": symbol, "count": len(group), "pnl": group["pnl"].sum(),
            "winRate": (group["pnl"] > 0).mean() * 100, "fees": group["fees"].sum(),
        }
        for symbol, group in frame.groupby("symbol")
    ]
    months = frame.assign(month=frame["closed_at"].dt.strftime("%Y-%m"))
    monthly = [
        {"month": month, "sold": group["sold_value"].sum(), "pnl": group["pnl"].sum()}
        for month, group in months.groupby("month")
    ]
    recent = frame.tail(100).iloc[::-1].copy()
    recent["closed_at"] = recent["closed_at"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "count": len(frame),
        "pnl": frame["pnl"].sum(),
        "winRate": len(wins) / len(frame) * 100,
        "averageWin": wins["pnl"].mean() if len(wins) else None,
        "averageLoss": losses["pnl"].mean() if len(losses) else None,
        "profitFactor": wins["pnl"].sum() / gross_loss if gross_loss > 0 else None,
        "maxDrawdown": min(drawdown, 0.0),
        "fees": frame["fees"].sum(),
        "sold": frame["sold_value"].sum(),
        "curve": [
            {"time": moment.strftime("%d/%m/%Y"), "pnl": value}
            for moment, value in zip(frame["closed_at"], cumulative)
        ],
        "bySymbol": by_symbol,
        "monthly": monthly,
        "trades": recent[["closed_at", "symbol", "quantity", "entry_price", "exit_price", "pnl",
                          "pnl_pct", "exit_reason"]].to_dict("records"),
    }
