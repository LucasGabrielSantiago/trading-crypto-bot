"""Uso: python main.py ui | backtest | validate | check | run"""
import argparse
import logging
import sys

import pandas as pd

from bot.backtest import evaluate, run_backtest, synthetic_candles, validate
from bot.config import load_settings
from bot.exchange import Exchange, create_testnet_client, fetch_public_history
from bot.runner import Portfolio, make_bot
from bot.storage import Storage
from bot.web import connect_history


def load_data(args, settings):
    settings = settings.for_symbol(settings.symbol_list[0])
    if args.synthetic:
        return synthetic_candles(), "dados sintéticos"
    if args.csv:
        return pd.read_csv(args.csv), args.csv
    data = fetch_public_history(settings.symbol, settings.timeframe, args.candles)
    return data.reset_index(drop=True), f"Binance {settings.symbol} {settings.timeframe}"


def report(result):
    print(f"Capital final:             {result.final_equity:.2f} USDT")
    print(f"Retorno:                   {result.return_pct:.2f}%")
    print(f"Comprar e segurar:         {result.buy_hold_pct:.2f}%")
    print(f"Exposição média:           {result.average_exposure_pct:.2f}%")
    print(f"Segurar, mesma exposição:  {result.matched_hold_pct:.2f}%")
    print(f"Queda máxima:              {result.max_drawdown_pct:.2f}%")
    print(f"Operações:                 {result.trades} ({result.win_rate_pct:.1f}% com ganho)")
    print(f"Taxas pagas:               {result.fees:.2f} USDT")
    print(f"Total vendido:             {result.sold_volume:.2f} USDT")


def backtest(args, settings):
    data, source = load_data(args, settings)
    settings = settings.for_symbol(settings.symbol_list[0])
    print(f"Fonte: {source} ({len(data)} candles), estratégia {settings.strategy}")
    report(run_backtest(data, settings, args.fee, args.slippage))


def validation(args, settings):
    data, source = load_data(args, settings)
    settings = settings.for_symbol(settings.symbol_list[0])
    print(f"Fonte: {source} ({len(data)} candles)")
    if settings.strategy != "ranges":
        # A busca de parâmetros existe só para ranges; as outras são testadas como estão.
        outcome = evaluate(data, settings, args.fee, args.slippage)
        print(f"Estratégia {settings.strategy}, sem busca de parâmetros\n")
        print("Primeiros 70%:")
        report(outcome["first"])
        print("\nÚltimos 30%:")
        report(outcome["second"])
        print(f"\nResultado: {'APROVADA para seguir na testnet' if outcome['approved'] else 'REPROVADA'}")
        return
    outcome = validate(data, settings, args.fee, args.slippage)
    if outcome is None:
        print("REPROVADA: nenhuma configuração gerou operações suficientes.")
        return
    print(f"Parâmetros escolhidos: {outcome['params']}\n")
    print("Primeiros 70% (usados para escolher):")
    report(outcome["development"])
    print("\nÚltimos 30% (confirmação):")
    report(outcome["holdout"])
    verdict = "APROVADA para seguir na testnet" if outcome["approved"] else "REPROVADA"
    print(f"\nResultado: {verdict}")
    print("Se mudar a estratégia depois de ver os últimos 30%, eles deixam de ser uma confirmação.")


def connected_exchange(settings):
    return Exchange(create_testnet_client(settings), settings.symbol, settings.timeframe)


def check(settings):
    exchange = connected_exchange(settings.for_symbol(settings.symbol_list[0]))
    exchange.load()
    base, quote = exchange.balances()
    print(f"Testnet conectada. {exchange.base}: {base} | {exchange.quote} livre: {quote}")
    print(f"Pares: {', '.join(settings.symbol_list)}")


def build_portfolio(settings):
    bots = []
    for symbol in settings.symbol_list:
        pair = settings.for_symbol(symbol)
        bots.append(make_bot(pair, connected_exchange(pair), Storage(pair.db_path)))
    return Portfolio(bots, settings.poll_seconds, connect_history())


def run(settings, show_panel):
    portfolio = build_portfolio(settings)
    try:
        if show_panel:
            from rich.live import Live

            from bot.panel import render_all

            with Live(render_all(portfolio.snapshots()), refresh_per_second=1) as live:
                portfolio.run(lambda data: live.update(render_all(data)))
        else:
            portfolio.run()
    except KeyboardInterrupt:
        print("Bot encerrado. Posições abertas continuam com o OCO na exchange.")
        return
    for symbol, reason in portfolio.halted.items():
        print(f"{symbol} parado: {reason}")
    sys.exit(1)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Bot de trading em testnet")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backtest", "validate"):
        sub = commands.add_parser(name)
        sub.add_argument("--synthetic", action="store_true")
        sub.add_argument("--csv")
        sub.add_argument("--candles", type=int, default=4380)
        sub.add_argument("--fee", type=float, default=0.001)
        sub.add_argument("--slippage", type=float, default=0.0005)
    commands.add_parser("check")
    commands.add_parser("ui")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--no-panel", action="store_true")
    args = parser.parse_args()

    if args.command == "ui":
        from bot.web import serve

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        print("Interface em http://127.0.0.1:8765 (feche esta janela para desligar)")
        serve()
        return

    settings = load_settings()
    panel = args.command == "run" and not args.no_panel
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        **({"filename": "bot.log", "encoding": "utf-8"} if panel else {}),
    )
    if args.command == "backtest":
        backtest(args, settings)
    elif args.command == "validate":
        validation(args, settings)
    elif args.command == "check":
        check(settings)
    else:
        run(settings, panel)


if __name__ == "__main__":
    main()
