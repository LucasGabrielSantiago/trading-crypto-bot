"""Regras de risco usadas igualmente no backtest e na operação em testnet."""


def position_size(equity, price, stop_loss_pct, risk_per_trade, max_position_pct, available):
    """Retorna a quantidade do ativo para que o stop custe no máximo o risco definido."""
    if equity <= 0 or price <= 0 or available <= 0:
        return 0.0
    notional = equity * risk_per_trade / stop_loss_pct
    notional = min(notional, equity * max_position_pct, available)
    return notional / price


def daily_limit_hit(day_start_equity, equity, max_daily_loss_pct) -> bool:
    return equity <= day_start_equity * (1 - max_daily_loss_pct)
