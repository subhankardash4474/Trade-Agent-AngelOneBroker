"""
CLI Dashboard Module
Real-time terminal dashboard using Rich library for monitoring
the trading agent's status, positions, and performance.
"""

import time
from datetime import datetime
from typing import Optional

import pytz
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

IST = pytz.timezone("Asia/Kolkata")


class Dashboard:
    """
    Real-time CLI dashboard for the trading agent.
    Uses the Rich library for rendering formatted terminal output.
    """

    def __init__(self, agent, refresh_interval: float = 5.0):
        self._agent = agent
        self._refresh = refresh_interval
        self._console = Console()

    def run(self):
        """Start the live-updating dashboard."""
        try:
            with Live(self._build_layout(), refresh_per_second=1 / self._refresh, console=self._console) as live:
                while self._agent._running:
                    live.update(self._build_layout())
                    time.sleep(self._refresh)
        except KeyboardInterrupt:
            pass

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=5),
        )
        layout["body"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1),
        )

        status = self._agent.get_status()
        layout["header"].update(self._header_panel(status))
        layout["left"].split_column(
            Layout(name="portfolio", ratio=1),
            Layout(name="positions", ratio=1),
        )
        layout["left"]["portfolio"].update(self._portfolio_panel(status))
        layout["left"]["positions"].update(self._positions_panel(status))
        layout["right"].split_column(
            Layout(name="risk", ratio=1),
            Layout(name="trades", ratio=1),
        )
        layout["right"]["risk"].update(self._risk_panel(status))
        layout["right"]["trades"].update(self._recent_trades_panel())
        layout["footer"].update(self._footer_panel(status))
        return layout

    def _header_panel(self, status: dict) -> Panel:
        mode = status.get("mode", "unknown").upper()
        mode_color = "green" if mode == "PAPER" else "red"
        market = "[green]OPEN[/]" if status.get("market_open") else "[red]CLOSED[/]"
        now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

        text = Text.from_markup(
            f"  [{mode_color}]{mode} MODE[/{mode_color}]  |  "
            f"Market: {market}  |  "
            f"Cycle: {status.get('cycle_count', 0)}  |  "
            f"{now}"
        )
        return Panel(text, title="AI Trading Agent - Indian Stock Market", border_style="blue")

    def _portfolio_panel(self, status: dict) -> Panel:
        portfolio = status.get("portfolio", {})
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="white", justify="right")

        total = portfolio.get("total_value", 0)
        cash = portfolio.get("cash", 0)
        unr = portfolio.get("unrealized_pnl", 0)
        real = portfolio.get("realized_pnl", 0)

        unr_color = "green" if unr >= 0 else "red"
        real_color = "green" if real >= 0 else "red"

        table.add_row("Total Value", f"₹{total:,.2f}")
        table.add_row("Cash", f"₹{cash:,.2f}")
        table.add_row("Unrealized PnL", f"[{unr_color}]₹{unr:,.2f}[/{unr_color}]")
        table.add_row("Realized PnL", f"[{real_color}]₹{real:,.2f}[/{real_color}]")
        table.add_row("Open Positions", str(portfolio.get("open_positions", 0)))
        table.add_row("Total Trades", str(portfolio.get("total_trades", 0)))

        return Panel(table, title="Portfolio", border_style="green")

    def _positions_panel(self, status: dict) -> Panel:
        table = Table(box=None, padding=(0, 1))
        table.add_column("Symbol", style="cyan")
        table.add_column("Side", style="white")
        table.add_column("Qty", justify="right")
        table.add_column("Entry", justify="right")
        table.add_column("Strategy", style="dim")

        for symbol, pos in self._agent.portfolio.positions.items():
            side_color = "green" if pos.side == "BUY" else "red"
            table.add_row(
                symbol,
                f"[{side_color}]{pos.side}[/{side_color}]",
                str(pos.quantity),
                f"₹{pos.entry_price:.2f}",
                pos.strategy,
            )

        if not self._agent.portfolio.positions:
            table.add_row("—", "—", "—", "—", "No open positions")

        return Panel(table, title="Open Positions", border_style="yellow")

    def _risk_panel(self, status: dict) -> Panel:
        risk = status.get("risk", {})
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="white", justify="right")

        daily_pnl = risk.get("daily_pnl", 0)
        pnl_color = "green" if daily_pnl >= 0 else "red"
        dd = risk.get("drawdown_pct", 0)
        dd_color = "green" if dd < 5 else ("yellow" if dd < 8 else "red")
        cb = risk.get("circuit_breaker", False)
        cb_text = "[red]ACTIVE[/]" if cb else "[green]OK[/]"

        table.add_row("Daily PnL", f"[{pnl_color}]₹{daily_pnl:,.2f}[/{pnl_color}]")
        table.add_row("Daily Trades", str(risk.get("daily_trades", 0)))
        table.add_row("Drawdown", f"[{dd_color}]{dd:.2f}%[/{dd_color}]")
        table.add_row("Peak Balance", f"₹{risk.get('peak_balance', 0):,.2f}")
        table.add_row("Open Positions", str(risk.get("open_positions", 0)))
        table.add_row("Circuit Breaker", cb_text)

        return Panel(table, title="Risk Management", border_style="red")

    def _recent_trades_panel(self) -> Panel:
        table = Table(box=None, padding=(0, 1))
        table.add_column("Time", style="dim")
        table.add_column("Symbol", style="cyan")
        table.add_column("Side")
        table.add_column("PnL", justify="right")
        table.add_column("Reason", style="dim")

        recent = self._agent.portfolio.trade_history[-8:]  # last 8 trades
        for trade in reversed(recent):
            pnl_color = "green" if trade.pnl >= 0 else "red"
            side_color = "green" if trade.side == "BUY" else "red"
            table.add_row(
                trade.exit_time.strftime("%H:%M"),
                trade.symbol,
                f"[{side_color}]{trade.side}[/{side_color}]",
                f"[{pnl_color}]₹{trade.pnl:,.2f}[/{pnl_color}]",
                trade.exit_reason,
            )

        if not recent:
            table.add_row("—", "—", "—", "—", "No trades yet")

        return Panel(table, title="Recent Trades", border_style="magenta")

    def _footer_panel(self, status: dict) -> Panel:
        strategies = ", ".join(status.get("strategies", []))
        instruments = ", ".join(status.get("instruments", []))
        return Panel(
            f"Strategies: {strategies}\nInstruments: {instruments}\n[dim]Press Ctrl+C to stop[/dim]",
            title="Configuration",
            border_style="dim",
        )


def print_snapshot(agent):
    """Print a one-time dashboard snapshot (non-live)."""
    console = Console()
    status = agent.get_status()
    portfolio = status.get("portfolio", {})
    risk = status.get("risk", {})

    console.print("\n[bold blue]═══ Trading Agent Status ═══[/bold blue]")
    console.print(f"  Mode: [bold]{status.get('mode', '?').upper()}[/bold]")
    console.print(f"  Market: {'[green]OPEN[/]' if status.get('market_open') else '[red]CLOSED[/]'}")
    console.print(f"  Balance: ₹{portfolio.get('total_value', 0):,.2f}")
    daily = risk.get("daily_pnl", 0)
    color = "green" if daily >= 0 else "red"
    console.print(f"  Daily PnL: [{color}]₹{daily:,.2f}[/{color}]")
    console.print(f"  Positions: {portfolio.get('open_positions', 0)}")
    console.print(f"  Trades: {portfolio.get('total_trades', 0)}")
    console.print()
