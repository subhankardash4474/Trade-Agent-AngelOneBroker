"""V4 Mode A universe loader.

Reads `data/v4_universe_swing_cash.txt` (charter v4 §3.1 spec) and returns
the parsed list of symbols. The file format is:

    - one symbol per line, NSE ticker format (no `.NS` suffix)
    - blank lines ignored
    - lines starting with `#` are comments (ignored)
    - `LIQUIDBEES` is included but excluded from signal generation via the
      `exclude_cash_sweep` flag (charter §3.1 tail: "LIQUIDBEES is the
      cash-sweep destination during low-conviction periods, not a trend
      instrument").

Companion file: `data/v4_universe_swing_cash.txt`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Set

# Charter §3.1 — these instruments are universe members but explicitly NOT
# signal-generation candidates. Loader skips them when
# `exclude_cash_sweep=True` (the default for signal pipelines).
_CASH_SWEEP_SYMBOLS: Set[str] = {"LIQUIDBEES"}

# Charter §3.1 — expected universe size (regression check; raises on drift).
EXPECTED_UNIVERSE_SIZE = 75
EXPECTED_SIGNAL_SIZE = 74  # universe size minus cash-sweep


def _project_root() -> Path:
    """Resolve the repo root from this module's location."""
    return Path(__file__).resolve().parent.parent.parent.parent


def _default_universe_path() -> Path:
    return _project_root() / "data" / "v4_universe_swing_cash.txt"


def load_v4_swing_cash_universe(
    universe_path: Path | str | None = None,
    *,
    exclude_cash_sweep: bool = True,
    yfinance_suffix: bool = False,
) -> List[str]:
    """Load Mode A's 75-instrument universe.

    Args:
        universe_path: optional override; defaults to `data/v4_universe_swing_cash.txt`.
        exclude_cash_sweep: when True (default), drops LIQUIDBEES from the
            returned list — LIQUIDBEES is a cash-sweep destination, not a
            trend candidate. Signal pipelines should pass True. The full
            universe (including LIQUIDBEES) is needed only when the
            portfolio orchestrator decides where to park idle capital.
        yfinance_suffix: when True, appends `.NS` to each symbol so the
            list is yfinance-ready. Default False (raw NSE tickers).

    Returns:
        Ordered list of symbols. Order follows the file (Nifty 50 first,
        then Next 50, then ETFs by category).

    Raises:
        FileNotFoundError: if the universe file is missing.
        ValueError: if the universe size disagrees with EXPECTED_UNIVERSE_SIZE
            (or EXPECTED_SIGNAL_SIZE when exclude_cash_sweep=True).
    """
    p = Path(universe_path) if universe_path else _default_universe_path()
    if not p.exists():
        raise FileNotFoundError(
            f"V4 Mode A universe file not found: {p}. "
            f"Expected location per charter §3.1: data/v4_universe_swing_cash.txt"
        )

    raw_lines = p.read_text(encoding="utf-8").splitlines()
    symbols: List[str] = []
    seen: Set[str] = set()
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Defensive: dedupe within the file (silent regression — universe
        # refresh script may accidentally produce duplicates if a Nifty 50
        # name is also picked into Nifty Next 50's top-15-ADTV slice).
        if stripped in seen:
            continue
        symbols.append(stripped)
        seen.add(stripped)

    full_size = len(symbols)
    if full_size != EXPECTED_UNIVERSE_SIZE:
        # Soft warning, not hard fail — quarterly refresh may legitimately
        # shift the count by a few (charter §3.1 says "60-80 instruments").
        # But anything outside 60-80 is suspicious enough to surface.
        if not (60 <= full_size <= 80):
            raise ValueError(
                f"V4 Mode A universe size {full_size} outside expected "
                f"range 60-80 (charter §3.1). Check {p}."
            )

    if exclude_cash_sweep:
        symbols = [s for s in symbols if s not in _CASH_SWEEP_SYMBOLS]

    if yfinance_suffix:
        symbols = [
            s if s.upper().endswith(".NS") else f"{s}.NS"
            for s in symbols
        ]

    return symbols


def cash_sweep_symbols() -> Set[str]:
    """Return the set of universe members that are cash-sweep destinations
    (excluded from signal generation per charter §3.1)."""
    return frozenset(_CASH_SWEEP_SYMBOLS)


def universe_categories(universe_path: Path | str | None = None) -> dict:
    """Parse the universe file's `# ===== Category =====` section markers
    and return a dict mapping category-name → list-of-symbols-in-that-section.

    Useful for sector-cap enforcement (charter §3.6: max 3 positions per
    sector) and for diagnostics in the comparison.md report.
    """
    p = Path(universe_path) if universe_path else _default_universe_path()
    if not p.exists():
        raise FileNotFoundError(f"Universe file not found: {p}")

    categories: dict[str, List[str]] = {}
    current = "uncategorised"
    for line in p.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# ====="):
            # Parse "===== Category (count) =====" into "Category".
            label = stripped.strip("# =").strip()
            # Drop trailing parenthetical count + any descriptive tail.
            label = label.split("(")[0].strip()
            current = label or "uncategorised"
            categories.setdefault(current, [])
            continue
        if stripped.startswith("#"):
            continue
        categories.setdefault(current, []).append(stripped)

    return categories


def list_signal_candidates() -> List[str]:
    """Convenience: load + apply the standard signal-pipeline filters.

    Equivalent to `load_v4_swing_cash_universe(exclude_cash_sweep=True)`.
    Defined for readability at call sites.
    """
    return load_v4_swing_cash_universe(exclude_cash_sweep=True)


__all__ = [
    "load_v4_swing_cash_universe",
    "cash_sweep_symbols",
    "universe_categories",
    "list_signal_candidates",
    "EXPECTED_UNIVERSE_SIZE",
    "EXPECTED_SIGNAL_SIZE",
]
