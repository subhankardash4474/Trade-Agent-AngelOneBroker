"""
Streamlit Dashboard
Modern web UI for monitoring the trading agent's performance,
open positions, equity curve, trade log, and risk metrics.

Run with: streamlit run monitoring/streamlit_app.py
"""

import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import streamlit as st
except ImportError:
    print("Streamlit not installed. Run: pip install streamlit")
    sys.exit(1)

import plotly.express as px
import plotly.graph_objects as go

from core.database import Database

# ── Page config ─────────────────────────────────────────────────
st.set_page_config(
    page_title="Trading Agent",
    page_icon="\U0001F4C8",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS for a cleaner look ───────────────────────────────
CUSTOM_CSS = """
<style>
    /* tighter top padding so the header sits nicely */
    .block-container {padding-top: 1.5rem; padding-bottom: 2rem;}

    /* KPI card styling */
    [data-testid="stMetric"] {
        background: linear-gradient(135deg, rgba(255,255,255,0.03), rgba(255,255,255,0.06));
        border: 1px solid rgba(128,128,128,0.18);
        padding: 14px 18px;
        border-radius: 12px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    }
    [data-testid="stMetricLabel"] {
        font-size: 0.78rem !important;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        opacity: 0.75;
    }
    [data-testid="stMetricValue"] {
        font-size: 1.55rem !important;
        font-weight: 600;
    }

    /* status pills */
    .pill {
        display: inline-block;
        padding: 3px 12px;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 600;
        margin-right: 8px;
        letter-spacing: 0.04em;
    }
    .pill-paper  {background: rgba(46,160,67,0.15);  color:#3fb950; border:1px solid #3fb95044;}
    .pill-live   {background: rgba(248,81,73,0.15);  color:#f85149; border:1px solid #f8514944;}
    .pill-open   {background: rgba(56,139,253,0.15); color:#58a6ff; border:1px solid #58a6ff44;}
    .pill-closed {background: rgba(139,148,158,0.18);color:#8b949e; border:1px solid #8b949e44;}

    /* tabs spacing */
    .stTabs [data-baseweb="tab-list"] {gap: 4px;}
    .stTabs [data-baseweb="tab"] {
        padding: 8px 18px;
        border-radius: 8px 8px 0 0;
    }

    /* small caption row */
    .meta-row {opacity: 0.65; font-size: 0.85rem;}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# Plotly theme (auto follow streamlit theme)
PLOTLY_TEMPLATE = "plotly_dark" if st.get_option("theme.base") == "dark" else "plotly_white"
COLOR_GREEN = "#3fb950"
COLOR_RED = "#f85149"
COLOR_BLUE = "#58a6ff"
COLOR_AMBER = "#d29922"


@st.cache_resource
def get_db():
    return Database()


# ── Cached data loaders ─────────────────────────────────────────
# Cache for 30s. Trading cycles are seconds-to-minutes so this is plenty
# fresh while eliminating repeated full-table scans on every Streamlit rerun.
_CACHE_TTL = 30


def _range_to_cutoff(choice: str):
    if choice == "All time":
        return None
    now = datetime.now()
    return {
        "Last 24 hours": now - timedelta(hours=24),
        "Last 7 days": now - timedelta(days=7),
        "Last 30 days": now - timedelta(days=30),
        "Today": datetime.combine(now.date(), datetime.min.time()),
    }.get(choice)


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def load_equity(range_choice: str) -> pd.DataFrame:
    """Load (and pre-resample) the equity curve, filtered by time range at SQL level."""
    db = get_db()
    cutoff = _range_to_cutoff(range_choice)
    sql = "SELECT timestamp, equity, cash, positions FROM equity_curve"
    params: list = []
    if cutoff is not None:
        sql += " WHERE timestamp >= ?"
        params.append(cutoff.isoformat())
    sql += " ORDER BY timestamp"

    with db._conn() as conn:
        df = pd.read_sql_query(sql, conn, params=params)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()

    # Pre-resample once for cheap chart rendering. Choose a frequency
    # proportional to the range so we always end up with O(few-hundred) points.
    if len(df) > 500:
        bucket = {
            "Today": "1min",
            "Last 24 hours": "5min",
            "Last 7 days": "15min",
            "Last 30 days": "1h",
        }.get(range_choice, "15min")
        df = df.resample(bucket).last().dropna(subset=["equity"])
    return df


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def load_trades(range_choice: str) -> pd.DataFrame:
    db = get_db()
    cutoff = _range_to_cutoff(range_choice)
    sql = "SELECT * FROM trades"
    params: list = []
    if cutoff is not None:
        sql += " WHERE exit_time >= ?"
        params.append(cutoff.isoformat())
    sql += " ORDER BY entry_time"
    with db._conn() as conn:
        df = pd.read_sql_query(sql, conn, params=params)
    return df


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def load_positions() -> list:
    try:
        return get_db().load_open_positions()
    except Exception:
        return []


def fmt_money(x: float) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"\u20B9{x:,.2f}"


def fmt_pct(x: float) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{x:+.2f}%"


def empty_placeholder(text: str, icon: str = "\U0001F4ED"):
    st.markdown(
        f"<div style='text-align:center; padding:48px; opacity:0.55;'>"
        f"<div style='font-size:2.2rem'>{icon}</div>"
        f"<div style='margin-top:8px;'>{text}</div></div>",
        unsafe_allow_html=True,
    )


# ────────────────────────────────────────────────────────────────
# Header & sidebar
# ────────────────────────────────────────────────────────────────
def render_sidebar() -> dict:
    st.sidebar.title("Trading Agent")
    st.sidebar.caption("Indian Stock Market \u00B7 Paper Mode")

    st.sidebar.divider()
    st.sidebar.subheader("Refresh")
    auto_refresh = st.sidebar.toggle("Auto-refresh", value=False)
    refresh_interval = st.sidebar.select_slider(
        "Interval (seconds)",
        options=[15, 30, 60, 120, 300, 600],
        value=60,
        disabled=not auto_refresh,
    )
    if st.sidebar.button("\U0001F504 Refresh now", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    if auto_refresh:
        st.sidebar.caption(f"Refreshes every {refresh_interval}s")
        st.markdown(
            f'<meta http-equiv="refresh" content="{refresh_interval}">',
            unsafe_allow_html=True,
        )

    st.sidebar.divider()
    st.sidebar.subheader("Filters")
    range_choice = st.sidebar.selectbox(
        "Time range",
        ["All time", "Last 24 hours", "Last 7 days", "Last 30 days", "Today"],
        index=0,
    )

    st.sidebar.divider()
    st.sidebar.caption(f"Last load: {datetime.now().strftime('%H:%M:%S')}")
    return {"range": range_choice}


def render_header(equity_df: pd.DataFrame, trades_df: pd.DataFrame, positions: list):
    market_open = is_market_open()
    mode_pill = "<span class='pill pill-paper'>PAPER</span>"
    market_pill = (
        "<span class='pill pill-open'>MARKET OPEN</span>"
        if market_open
        else "<span class='pill pill-closed'>MARKET CLOSED</span>"
    )
    last_seen = "—"
    if not equity_df.empty:
        ts = equity_df.index.max()
        last_seen = pd.to_datetime(ts).strftime("%Y-%m-%d %H:%M:%S")

    st.markdown(
        f"""
        <div style='display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; margin-bottom:0.5rem;'>
            <div>
                <div style='font-size:1.65rem; font-weight:700;'>\U0001F4C8 AI Trading Agent</div>
                <div class='meta-row'>Last snapshot: {last_seen} \u00B7 {len(trades_df) if trades_df is not None else 0} trades \u00B7 {len(positions)} open positions</div>
            </div>
            <div>{mode_pill}{market_pill}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def is_market_open() -> bool:
    """NSE hours roughly 09:15 - 15:30 IST, Mon-Fri."""
    try:
        import pytz
        now = datetime.now(pytz.timezone("Asia/Kolkata"))
    except Exception:
        now = datetime.now()
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


# ────────────────────────────────────────────────────────────────
# KPI row
# ────────────────────────────────────────────────────────────────
def render_kpis(equity_df: pd.DataFrame, trades_df: pd.DataFrame, positions: list):
    c1, c2, c3, c4, c5 = st.columns(5)

    if not equity_df.empty:
        latest = float(equity_df["equity"].iloc[-1])
        initial = float(equity_df["equity"].iloc[0])
        ret_pct = (latest - initial) / initial * 100 if initial else 0.0
        c1.metric("Portfolio Value", fmt_money(latest), delta=fmt_pct(ret_pct))
        cash_now = float(equity_df["cash"].iloc[-1]) if "cash" in equity_df.columns else 0.0
        invested = max(latest - cash_now, 0)
        c2.metric("Cash", fmt_money(cash_now), delta=f"Invested {fmt_money(invested)}", delta_color="off")
    else:
        c1.metric("Portfolio Value", fmt_money(10000))
        c2.metric("Cash", fmt_money(10000))

    if trades_df is not None and not trades_df.empty:
        total_pnl = float(trades_df["pnl"].sum())
        wins = int((trades_df["pnl"] > 0).sum())
        total = len(trades_df)
        win_rate = wins / total * 100 if total > 0 else 0.0

        # today's P&L
        today_pnl = 0.0
        if "exit_time" in trades_df.columns:
            today = datetime.now().date()
            ex = pd.to_datetime(trades_df["exit_time"], errors="coerce")
            today_pnl = float(trades_df.loc[ex.dt.date == today, "pnl"].sum())

        c3.metric(
            "Total P&L",
            fmt_money(total_pnl),
            delta=f"Today {fmt_money(today_pnl)}",
            delta_color="normal" if today_pnl >= 0 else "inverse",
        )
        c4.metric("Win Rate", f"{win_rate:.1f}%", delta=f"{wins}/{total}", delta_color="off")
    else:
        c3.metric("Total P&L", fmt_money(0))
        c4.metric("Win Rate", "—")

    c5.metric("Open Positions", str(len(positions)))


# ────────────────────────────────────────────────────────────────
# Charts
# ────────────────────────────────────────────────────────────────
def equity_chart(equity_df: pd.DataFrame) -> go.Figure:
    """Smoothed equity curve. Data is already pre-resampled in load_equity()."""
    df = equity_df
    initial = float(df["equity"].iloc[0])
    return_pct = (df["equity"] - initial) / initial * 100

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df.index, y=df["equity"],
        mode="lines",
        name="Equity",
        line=dict(color=COLOR_BLUE, width=2),
        fill="tozeroy",
        fillcolor="rgba(88,166,255,0.10)",
        hovertemplate="<b>%{x|%b %d, %H:%M}</b><br>Equity: \u20B9%{y:,.2f}<br>Return: %{customdata:+.2f}%<extra></extra>",
        customdata=return_pct,
    ))
    fig.add_hline(y=initial, line_dash="dot", line_color="rgba(128,128,128,0.5)",
                  annotation_text=f"Start \u20B9{initial:,.0f}", annotation_position="bottom right")
    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        height=380,
        margin=dict(l=10, r=10, t=20, b=10),
        showlegend=False,
        hovermode="x unified",
        xaxis=dict(showgrid=False),
        yaxis=dict(title="Portfolio Value (\u20B9)", gridcolor="rgba(128,128,128,0.15)"),
    )
    return fig


def drawdown_chart(equity_df: pd.DataFrame) -> go.Figure:
    """Drawdown % chart. Data is already pre-resampled in load_equity()."""
    df = equity_df
    running_max = df["equity"].cummax()
    drawdown_pct = (df["equity"] - running_max) / running_max * 100

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df.index, y=drawdown_pct,
        mode="lines",
        line=dict(color=COLOR_RED, width=1.5),
        fill="tozeroy",
        fillcolor="rgba(248,81,73,0.18)",
        hovertemplate="<b>%{x|%b %d, %H:%M}</b><br>Drawdown: %{y:.2f}%<extra></extra>",
        name="Drawdown",
    ))
    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        height=220,
        margin=dict(l=10, r=10, t=20, b=10),
        showlegend=False,
        hovermode="x unified",
        xaxis=dict(showgrid=False),
        yaxis=dict(title="Drawdown (%)", gridcolor="rgba(128,128,128,0.15)"),
    )
    return fig


def daily_pnl_chart(trades_df: pd.DataFrame) -> go.Figure:
    df = trades_df.copy()
    df["exit_date"] = pd.to_datetime(df["exit_time"], errors="coerce").dt.date
    daily = df.groupby("exit_date")["pnl"].sum().reset_index()
    daily["color"] = np.where(daily["pnl"] >= 0, COLOR_GREEN, COLOR_RED)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=daily["exit_date"], y=daily["pnl"],
        marker_color=daily["color"],
        hovertemplate="<b>%{x}</b><br>P&L: \u20B9%{y:,.2f}<extra></extra>",
    ))
    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        height=320,
        margin=dict(l=10, r=10, t=20, b=10),
        showlegend=False,
        xaxis=dict(title="Date", showgrid=False),
        yaxis=dict(title="Daily P&L (\u20B9)", gridcolor="rgba(128,128,128,0.15)", zeroline=True),
    )
    return fig


def pnl_distribution_chart(trades_df: pd.DataFrame) -> go.Figure:
    pnls = trades_df["pnl"].dropna()
    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=pnls,
        nbinsx=30,
        marker=dict(
            color=np.where(pnls >= 0, COLOR_GREEN, COLOR_RED),
            line=dict(color="rgba(0,0,0,0.2)", width=0.5),
        ),
        hovertemplate="Range: %{x}<br>Trades: %{y}<extra></extra>",
    ))
    fig.add_vline(x=0, line_dash="dot", line_color="rgba(128,128,128,0.6)")
    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        height=320,
        margin=dict(l=10, r=10, t=20, b=10),
        showlegend=False,
        bargap=0.05,
        xaxis=dict(title="P&L per trade (\u20B9)", gridcolor="rgba(128,128,128,0.15)"),
        yaxis=dict(title="Trade count", gridcolor="rgba(128,128,128,0.15)"),
    )
    return fig


def strategy_chart(strategy_stats: pd.DataFrame) -> go.Figure:
    s = strategy_stats.copy().reset_index().rename(columns={"index": "strategy"})
    s = s.sort_values("total_pnl", ascending=True)
    s["color"] = np.where(s["total_pnl"] >= 0, COLOR_GREEN, COLOR_RED)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=s["strategy"], x=s["total_pnl"],
        orientation="h",
        marker_color=s["color"],
        text=[f"\u20B9{v:,.0f}" for v in s["total_pnl"]],
        textposition="outside",
        hovertemplate="<b>%{y}</b><br>P&L: \u20B9%{x:,.2f}<br>Trades: %{customdata[0]}<br>Win rate: %{customdata[1]:.1f}%<extra></extra>",
        customdata=s[["trades", "win_rate"]].values,
    ))
    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        height=max(220, 50 * len(s) + 80),
        margin=dict(l=10, r=40, t=20, b=10),
        showlegend=False,
        xaxis=dict(title="Total P&L (\u20B9)", gridcolor="rgba(128,128,128,0.15)", zeroline=True),
        yaxis=dict(title=""),
    )
    return fig


# ────────────────────────────────────────────────────────────────
# Tabs
# ────────────────────────────────────────────────────────────────
def tab_overview(equity_df: pd.DataFrame, trades_df: pd.DataFrame):
    if equity_df.empty:
        empty_placeholder(
            "No equity data yet. Start the trading agent to begin recording snapshots.",
            icon="\U0001F4C8",
        )
        return

    st.subheader("Equity Curve")
    st.caption("Total portfolio value over time (cash + mark-to-market positions). Resampled to reduce intraday noise.")
    st.plotly_chart(equity_chart(equity_df), width="stretch",
                    config={"displayModeBar": False}, key="overview_equity")

    st.subheader("Drawdown")
    st.caption("Percentage decline from the most recent peak.")
    st.plotly_chart(drawdown_chart(equity_df), width="stretch",
                    config={"displayModeBar": False}, key="overview_drawdown")


def tab_trades(trades_df: pd.DataFrame):
    if trades_df is None or trades_df.empty:
        empty_placeholder("No trades recorded yet.", icon="\U0001F4DD")
        return

    left, right = st.columns([2, 1])
    with left:
        st.subheader("Daily P&L")
        st.plotly_chart(daily_pnl_chart(trades_df), width="stretch",
                        config={"displayModeBar": False}, key="trades_daily_pnl")
    with right:
        st.subheader("P&L Distribution")
        st.plotly_chart(pnl_distribution_chart(trades_df), width="stretch",
                        config={"displayModeBar": False}, key="trades_pnl_dist")

    st.subheader("Trade Log")
    display_cols = [
        "exit_time", "symbol", "side", "strategy",
        "entry_price", "exit_price", "quantity",
        "pnl", "pnl_pct", "exit_reason",
    ]
    avail = [c for c in display_cols if c in trades_df.columns]
    df_show = trades_df[avail].copy()
    if "exit_time" in df_show.columns:
        df_show["exit_time"] = pd.to_datetime(df_show["exit_time"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
    df_show = df_show.sort_index(ascending=False)

    st.dataframe(
        df_show,
        width="stretch",
        height=420,
        column_config={
            "pnl": st.column_config.NumberColumn("P&L (\u20B9)", format="\u20B9%.2f"),
            "pnl_pct": st.column_config.NumberColumn("P&L %", format="%.2f%%"),
            "entry_price": st.column_config.NumberColumn("Entry", format="\u20B9%.2f"),
            "exit_price": st.column_config.NumberColumn("Exit", format="\u20B9%.2f"),
        },
    )


def tab_strategies(trades_df: pd.DataFrame):
    if trades_df is None or trades_df.empty or "strategy" not in trades_df.columns:
        empty_placeholder("No strategy data available.", icon="\U0001F9E0")
        return

    stats = trades_df.groupby("strategy").agg(
        trades=("pnl", "count"),
        total_pnl=("pnl", "sum"),
        avg_pnl=("pnl", "mean"),
        win_rate=("pnl", lambda x: (x > 0).mean() * 100),
    ).round(2)

    left, right = st.columns([3, 2])
    with left:
        st.subheader("P&L by Strategy")
        st.plotly_chart(strategy_chart(stats), width="stretch",
                        config={"displayModeBar": False}, key="strategies_pnl")
    with right:
        st.subheader("Strategy Stats")
        st.dataframe(
            stats.sort_values("total_pnl", ascending=False),
            width="stretch",
            column_config={
                "trades": st.column_config.NumberColumn("Trades", format="%d"),
                "total_pnl": st.column_config.NumberColumn("Total P&L", format="\u20B9%.2f"),
                "avg_pnl": st.column_config.NumberColumn("Avg P&L", format="\u20B9%.2f"),
                "win_rate": st.column_config.NumberColumn("Win Rate", format="%.1f%%"),
            },
        )


def tab_positions(positions: list):
    if not positions:
        empty_placeholder("No open positions.", icon="\U0001F4BC")
        return
    df = pd.DataFrame(positions)
    show_cols = [c for c in [
        "symbol", "side", "quantity", "entry_price",
        "stop_loss", "take_profit", "strategy", "entry_time", "regime",
    ] if c in df.columns]
    df = df[show_cols]
    if "entry_time" in df.columns:
        df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")

    st.subheader(f"Open Positions ({len(df)})")
    st.dataframe(
        df,
        width="stretch",
        column_config={
            "entry_price": st.column_config.NumberColumn("Entry", format="\u20B9%.2f"),
            "stop_loss": st.column_config.NumberColumn("Stop", format="\u20B9%.2f"),
            "take_profit": st.column_config.NumberColumn("Target", format="\u20B9%.2f"),
            "quantity": st.column_config.NumberColumn("Qty", format="%d"),
        },
    )


def tab_risk(equity_df: pd.DataFrame, trades_df: pd.DataFrame):
    if (trades_df is None or trades_df.empty) and equity_df.empty:
        empty_placeholder("No risk data yet.", icon="\u26A0\uFE0F")
        return

    # Drawdown from equity (more accurate than from trades)
    max_dd_pct = 0.0
    max_dd_abs = 0.0
    if not equity_df.empty:
        eq = equity_df["equity"].dropna()
        running_max = eq.cummax()
        dd = (eq - running_max) / running_max * 100
        dd_abs = running_max - eq
        max_dd_pct = float(dd.min()) if not dd.empty else 0.0
        max_dd_abs = float(dd_abs.max()) if not dd_abs.empty else 0.0

    sharpe = 0.0
    profit_factor = 0.0
    avg_pnl = 0.0
    expectancy = 0.0

    if trades_df is not None and not trades_df.empty:
        pnls = trades_df["pnl"].dropna()
        # Sharpe from daily returns of equity (preferred). Fallback to trade-level.
        if not equity_df.empty:
            daily_eq = equity_df["equity"].resample("1D").last().dropna()
            daily_ret = daily_eq.pct_change().dropna()
            if not daily_ret.empty and daily_ret.std() > 0:
                sharpe = float(daily_ret.mean() / daily_ret.std() * np.sqrt(252))
        elif pnls.std() > 0:
            sharpe = float(pnls.mean() / pnls.std() * np.sqrt(252))

        gross_profit = float(pnls[pnls > 0].sum())
        gross_loss = float(abs(pnls[pnls <= 0].sum()))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        avg_pnl = float(pnls.mean())

        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]
        win_rate = len(wins) / len(pnls) if len(pnls) else 0
        avg_win = wins.mean() if len(wins) else 0
        avg_loss = losses.mean() if len(losses) else 0
        expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Max Drawdown", fmt_money(max_dd_abs), delta=f"{max_dd_pct:.2f}%", delta_color="off")
    r2.metric("Sharpe Ratio", f"{sharpe:.2f}")
    r3.metric(
        "Profit Factor",
        "\u221E" if profit_factor == float("inf") else f"{profit_factor:.2f}",
    )
    r4.metric("Expectancy / trade", fmt_money(expectancy))

    if not equity_df.empty:
        st.subheader("Drawdown over time")
        st.plotly_chart(drawdown_chart(equity_df), width="stretch",
                        config={"displayModeBar": False}, key="risk_drawdown")


# ────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────
def main():
    filters = render_sidebar()

    # Cached + filtered at SQL level — repeated reruns within 30s are instant.
    equity_df = load_equity(filters["range"])
    trades_df = load_trades(filters["range"])
    positions = load_positions()

    render_header(equity_df, trades_df, positions)
    render_kpis(equity_df, trades_df, positions)
    st.divider()

    tabs = st.tabs(["\U0001F4C8 Overview", "\U0001F4DD Trades", "\U0001F9E0 Strategies", "\U0001F4BC Positions", "\u26A0\uFE0F Risk"])
    with tabs[0]:
        tab_overview(equity_df, trades_df)
    with tabs[1]:
        tab_trades(trades_df)
    with tabs[2]:
        tab_strategies(trades_df)
    with tabs[3]:
        tab_positions(positions)
    with tabs[4]:
        tab_risk(equity_df, trades_df)

    st.divider()
    st.caption("AI Trading Agent for Indian Stock Market \u00B7 Paper Trading")


if __name__ == "__main__":
    main()
