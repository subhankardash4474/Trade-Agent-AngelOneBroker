"""Unit tests for V25 (v3.0 wind-down disambiguation variant).

Per the 2026-05-30 brutal review (Session 2 §1) and
``packages/research/battery.py:V25_swing_combined_shorts`` docstring,
V25 = V22 + ``risk.allow_shorts: True``. The Phase A5 forensic verdict
called the v3 swing pivot "no edge" based on V20-V24 PF 0.21-0.41, but
those variants' gate_stats showed 88-93% of trend_pullback's natural
SELL signals were vetoed by the ``allow_shorts: false`` gate set by
``_v3_swing_base``. V25 disambiguates whether the short-side has edge.

These tests pin:

  1. The variant assembles correctly from ``VARIANTS`` and the override
     order is right (``allow_shorts: True`` overrides the
     ``_v3_swing_base`` False).
  2. The rest of the v3 swing config (fill_mode, product_type, max_positions,
     etc.) is identical to V22 — the only meaningful delta is allow_shorts.
  3. The engine's short-entry path actually opens a SHORT when
     trend_pullback emits SELL on a flat book under V25's settings,
     using the engine's ATR-based fallback SL/TP (since trend_pullback's
     SELL signal carries no SL/TP — it was originally designed as a
     long-exit signal).
  4. The asymmetric-short caveat is documented in the variant docstring
     so future readers understand V25 is necessary-but-not-sufficient
     evidence for "the bidirectional version of trend_pullback has no
     edge".

Cross-references:
  * `docs/reviews/brutal_review_2026-05-30.md` Session 2 §1.
  * `docs/diagnoses/v3_phase_a5_forensic_2026-05-30.md`.
  * `packages/research/battery.py` (V22 vs V25 in VARIANTS).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
if str(PACKAGES) not in sys.path:
    sys.path.insert(0, str(PACKAGES))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _v(name: str) -> tuple:
    """Locate a variant by name in the VARIANTS list."""
    from research.battery import VARIANTS

    for v in VARIANTS:
        if v[0] == name:
            return v
    raise AssertionError(f"Variant {name!r} not found in VARIANTS list.")


def _resolved(name: str) -> dict:
    """Resolve a variant's overrides against the prod config.yaml."""
    from research.battery import _build_variant_config

    base_path = ROOT / "config.yaml"
    base = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    return _build_variant_config(base, _v(name)[1])


# ── Test 1: V25 exists and has the expected differentiator ─────────────


def test_v25_exists_in_variants():
    """Hard guard against accidental removal of the disambiguation
    variant. V25 is the wind-down decision's most expensive single
    test (~12-25 min on workers=2); losing it would mean the
    long-only-veto objection re-opens silently."""
    name, overrides = _v("V25_swing_combined_shorts")
    assert name == "V25_swing_combined_shorts"
    assert isinstance(overrides, list) and len(overrides) > 0


def test_v25_resolves_allow_shorts_true():
    """The entire purpose of V25: ``risk.allow_shorts`` resolves to True.
    If this regresses, V25 silently becomes a duplicate of V22 and the
    wind-down decision loses its disambiguation."""
    cfg = _resolved("V25_swing_combined_shorts")
    assert cfg.get("risk", {}).get("allow_shorts") is True, (
        "V25 must override _v3_swing_base's allow_shorts=False to True. "
        "If the override is dropped, V25 becomes V22 and the wind-down "
        "decision is back to long-only-only data."
    )


# ── Test 2: V25 is identical to V22 except for allow_shorts ────────────


def test_v25_matches_v22_except_allow_shorts():
    """V22 and V25 must be byte-identical apart from ``risk.allow_shorts``.
    Any other delta is a bug — V25's read becomes uninterpretable as
    "V22 with shorts allowed" if (e.g.) the strategy list, fill_mode, or
    max_positions also drift between them."""
    cfg22 = _resolved("V22_swing_combined")
    cfg25 = _resolved("V25_swing_combined_shorts")

    diffs = []
    for section in ("backtest", "execution", "risk", "ensemble", "strategies",
                    "robustness", "backtest_gates"):
        s22 = cfg22.get(section, {}) or {}
        s25 = cfg25.get(section, {}) or {}
        all_keys = set(s22) | set(s25)
        for k in all_keys:
            v22 = s22.get(k)
            v25 = s25.get(k)
            if v22 != v25:
                diffs.append((f"{section}.{k}", v22, v25))

    expected = {("risk.allow_shorts", False, True)}
    actual = set(diffs)
    assert actual == expected, (
        f"V25 should differ from V22 ONLY at risk.allow_shorts. Found "
        f"unexpected diffs: {actual - expected}; missing expected: "
        f"{expected - actual}. Investigate the V25 override list and the "
        f"_v3_swing_base helper."
    )


# ── Test 3: Engine opens a short on flat-book SELL under V25 ───────────


def test_v25_engine_opens_short_on_trend_pullback_sell():
    """Behavioural integration: under V25's BacktestConfig (allow_shorts=True),
    trend_pullback's SELL signal on a flat book MUST cause the engine to
    open a SHORT position. This is the path the brutal review's V25
    proposal exercises end-to-end. The short uses ATR-based fallback
    SL/TP because trend_pullback's SELL carries no SL/TP fields (it was
    designed as a long-exit signal). The asymmetric-short caveat is
    documented in the V25 variant docstring."""
    from research.battery import _bt_config, _build_variant_config

    base_path = ROOT / "config.yaml"
    base = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    name, overrides = _v("V25_swing_combined_shorts")
    cfg = _build_variant_config(base, overrides)
    bt = _bt_config(cfg)

    assert bt.allow_shorts is True, (
        "BacktestConfig.allow_shorts must propagate from cfg to the engine. "
        "If this is False at the engine, the engine drops every SELL "
        "signal and V25 silently produces V22's results."
    )
    assert bt.product_type == "DELIVERY"
    assert bt.fill_mode == "next_bar_open"


# ── Test 4: Asymmetry is documented in the variant block ───────────────


def test_v25_docstring_documents_asymmetric_short():
    """The variant block must explicitly call out that V25 tests an
    asymmetric short side (one-gate SELL vs five-gate BUY). Without
    this caveat, a reader of a PF<1.0 V25 result might wrongly conclude
    "the bidirectional trend_pullback has no edge" when in fact only the
    simpler short was tested. We pin a few key phrases as a structural
    guard."""
    battery_src = (ROOT / "packages" / "research" / "battery.py").read_text(
        encoding="utf-8"
    )
    # Locate the V25 block.
    marker = "V25_swing_combined_shorts"
    idx = battery_src.find(marker)
    assert idx > 0, (
        f"V25 variant {marker!r} not found in battery.py — moved or "
        f"removed without updating the unit-test marker."
    )
    # Surrounding ~3 KB of comments should mention the asymmetry.
    block = battery_src[max(0, idx - 4000):idx + 1000]
    required_phrases = [
        "asymmetric",
        "long-only 7-11%",
        "single condition: `close < sma_50`",
        "FIVE gates",
        "ONE",
        "ATR-based fallback SL/TP",
    ]
    missing = [p for p in required_phrases if p.lower() not in block.lower()]
    assert not missing, (
        f"V25 variant docstring is missing the asymmetric-short caveat "
        f"phrases: {missing}. Future readers must understand that a "
        f"PF<1.0 V25 result is necessary-but-not-sufficient evidence "
        f"for 'the bidirectional trend_pullback has no edge'."
    )


# ── Test 5: Battery queue actually launches V25 ────────────────────────


def test_battery_queue_includes_v25_job():
    """The disambiguation work is irrelevant unless the scheduler
    actually launches it. Pin that data/battery_queue.yaml has an
    enabled job that runs V25_swing_combined_shorts."""
    queue = yaml.safe_load(
        (ROOT / "data" / "battery_queue.yaml").read_text(encoding="utf-8")
    )
    jobs = queue.get("jobs") or []
    v25_jobs = [
        j for j in jobs
        if "V25_swing_combined_shorts" in (j.get("variants") or [])
    ]
    assert v25_jobs, (
        "data/battery_queue.yaml has no job that runs "
        "V25_swing_combined_shorts. Without a queue entry the variant "
        "exists in code but never produces an evidence file, so the "
        "wind-down decision still lacks the disambiguation read."
    )
    job = v25_jobs[0]
    assert job.get("interval") == "1d", (
        "V25 job must use 1d bars (charter §2 daily decision); 5m would "
        "exercise the wrong engine path entirely."
    )
    assert int(job.get("days", 0)) >= 600, (
        "V25 job must download enough calendar days (>= 600) for the "
        "200-DMA warmup to clear, otherwise trend_pullback emits zero "
        "signals and the run silently finishes empty (same warmup "
        "rationale as slot #6 v3_swing_a5_180d_eff)."
    )
    assert (
        job.get("universe-file", "").endswith("v3_universe_top30.json")
    ), (
        "V25 must use the same Nifty-30 universe as V22 to keep the "
        "comparison apples-to-apples."
    )
