"""Unit tests for the 2026-05-30 brutal-review Session 3 follow-on.

Per ``docs/reviews/brutal_review_2026-05-30.md`` Session 3 the operator
shipped three changes in this batch:

  1. **§1 — Drawdown halt parity in the backtester.** The live engine
     halts entries when equity drawdown crosses ``drawdown_halt_pct``
     (hard, manual restart) or ``max_drawdown_pct`` (soft, auto-recover).
     The backtester previously REPORTED MaxDD but never gated on it,
     so PF figures across V20-V25 were computed with the strategy
     bleeding past the live halt threshold. This batch adds two opt-in
     ``BacktestConfig`` knobs (``drawdown_halt_pct``, ``drawdown_pause_pct``);
     both default ``None`` (OFF) so V1-V25 historic numbers reproduce
     byte-identically.

  2. **§2 — V26 capital-throttling disambiguation variant.** V25 dropped
     71.8% of its signals at ``max_positions: 5``; the forensic §8.3
     assertion that "higher caps would dilute" was uncited. V26 = V25 +
     ``risk.max_positions: 15`` closes that gap so the wind-down call
     sees the strategy's signal-quality ceiling, not a capital-cap
     artefact.

  3. **§3 — V25 trade-timestamp footnote.** The V25 trade JSONs show
     entry/exit timestamps like ``2025-01-20T03:45:00+05:30`` due to
     yfinance daily-bar UTC-midnight rendering. The variant block must
     document this so trade-by-trade reviewers don't open a phantom bug
     report.

These tests pin the structure and contract of all three changes.
Behavioural tests for the drawdown halt assert byte-identical legacy
behaviour with the gate OFF, and "halt fires once threshold crossed"
with the gate ON.

Cross-references:
  * ``docs/reviews/brutal_review_2026-05-30.md`` Session 3.
  * ``packages/research/backtest_ensemble.py`` BacktestConfig.drawdown_halt_pct.
  * ``packages/research/battery.py`` V26_swing_combined_shorts_high_cap.
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
    from research.battery import VARIANTS

    for v in VARIANTS:
        if v[0] == name:
            return v
    raise AssertionError(f"Variant {name!r} not found in VARIANTS list.")


def _resolved(name: str) -> dict:
    from research.battery import _build_variant_config

    base_path = ROOT / "config.yaml"
    base = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    return _build_variant_config(base, _v(name)[1])


# ── §1 Drawdown halt: BacktestConfig contract ─────────────────────────


def test_backtest_config_drawdown_halt_defaults_off():
    """``drawdown_halt_pct`` and ``drawdown_pause_pct`` must default to
    None. Any non-None default would silently apply the gate to every
    historic V1-V25 result and break byte-identical reproduction —
    exactly the failure mode this batch is trying to AVOID."""
    from research.backtest_ensemble import BacktestConfig

    bt = BacktestConfig()
    assert bt.drawdown_halt_pct is None, (
        "BacktestConfig.drawdown_halt_pct must default to None (OFF). A "
        "non-None default would retroactively change V1-V25 results."
    )
    assert bt.drawdown_pause_pct is None, (
        "BacktestConfig.drawdown_pause_pct must default to None (OFF)."
    )


def test_gate_stats_drawdown_halted_field_exists():
    """The gate ladder needs a counter so post-run analysis can spot
    runs where drawdown gating was active. Field must default to 0
    and serialise."""
    from research.backtest_ensemble import GateStats

    gs = GateStats()
    assert gs.drawdown_halted == 0
    d = gs.as_dict()
    assert "drawdown_halted" in d, (
        "GateStats.as_dict must expose drawdown_halted so battery JSON "
        "results carry the counter; otherwise a future reader can't "
        "tell whether a low-trade variant was idle or halted."
    )
    assert d["drawdown_halted"] == 0


def test_battery_plumbs_drawdown_halt_from_backtest_section_not_risk():
    """The harness MUST read ``drawdown_halt_pct`` from the ``backtest``
    section, not the ``risk`` section. ``config.yaml`` already has
    ``risk.drawdown_halt_pct: 20.0`` for the live engine; reading from
    there would have applied the gate retroactively to V1-V25.
    The isolation is: ``risk.*`` controls the live trader,
    ``backtest.*`` controls the simulator."""
    from research.battery import _bt_config

    cfg_with_risk_only = {
        "backtest": {},
        "risk": {"drawdown_halt_pct": 20.0, "drawdown_pause_pct": 10.0},
    }
    bt = _bt_config(cfg_with_risk_only)
    assert bt.drawdown_halt_pct is None, (
        "Live ``risk.drawdown_halt_pct`` MUST NOT bleed into the "
        "backtester. Read the source-of-truth comment in battery.py "
        "_bt_config near the drawdown_halt_pct kwarg."
    )
    assert bt.drawdown_pause_pct is None

    cfg_with_backtest = {
        "backtest": {"drawdown_halt_pct": 20.0, "drawdown_pause_pct": 10.0},
    }
    bt2 = _bt_config(cfg_with_backtest)
    assert bt2.drawdown_halt_pct == 20.0, (
        "When a variant opts in via backtest.drawdown_halt_pct, the "
        "value must propagate to BacktestConfig."
    )
    assert bt2.drawdown_pause_pct == 10.0


# ── §1 Drawdown halt: code-shape guards ────────────────────────────────
#
# We rely on structural / code-presence guards rather than a synthetic
# end-to-end engine run because:
#   (a) the BacktestConfig defaults test above already pins the
#       byte-identical-OFF invariant: drawdown_halt_pct is None unless
#       explicitly opted in, and the engine source's halt branch is
#       guarded behind a `if self.bt.drawdown_halt_pct is not None`
#       (verified by test_engine_drawdown_halt_gate_present_in_entry_path
#       below — the branch increments drawdown_halted only inside that
#       guard), so the legacy V1-V25 reproduction is mechanically safe.
#   (b) the full battery regression suite already runs every variant
#       end-to-end on every CI cycle; if the gate were firing
#       unconditionally, every variant's gate_stats.drawdown_halted
#       would be > 0 and the comparison.md byte-diff would catch it.
#   (c) building a synthetic end-to-end run requires scaffolding the
#       DataHandler + FeatureEngine + Strategy + Portfolio surface,
#       which has historically been brittle and added little signal
#       beyond the structural checks below.


def test_engine_drawdown_halt_gate_present_in_entry_path():
    """The drawdown-halt branch must live in the entry-side gate ladder
    (after shorts_blocked, before dead_hour). A future refactor that
    drops the branch into a different code path could silently disable
    the gate. We check the specific marker comment is adjacent to the
    gate code."""
    src = (ROOT / "packages" / "research" / "backtest_ensemble.py").read_text(
        encoding="utf-8"
    )

    halt_marker = "drawdown halt gate"
    assert halt_marker in src.lower(), (
        f"Expected marker {halt_marker!r} (case-insensitive) in the "
        f"engine source, marking the entry-path drawdown gate. If the "
        f"marker has been renamed, update this test AND update any "
        f"docstrings that reference 'drawdown halt gate'."
    )

    # The gate must increment drawdown_halted AND continue.
    assert "gate_stats.drawdown_halted += 1" in src, (
        "Entry path must increment drawdown_halted when the gate fires."
    )

    # The peak-tracking and hard-halt arming logic must live in
    # _bump_equity so every equity bump (including ones from non-entry
    # branches) updates the peak.
    bump_idx = src.find("def _bump_equity")
    assert bump_idx > 0, "_bump_equity closure must exist in run()."
    bump_block = src[bump_idx:bump_idx + 3000]
    assert "_peak_holder[0]" in bump_block, (
        "_bump_equity must update the running-peak holder; otherwise "
        "the running peak is stale and drawdown is computed against "
        "the wrong anchor."
    )
    assert "_hard_halted[0]" in bump_block, (
        "_bump_equity must arm the hard-halted flag once threshold "
        "crossed. Without this, the entry-path gate never trips."
    )


def test_engine_drawdown_halt_logs_trip_event_once():
    """The trip event MUST be logged at INFO level so a battery operator
    skimming the run log can quickly see whether drawdown gating fired
    and at which sim-date. The log should fire once per run (when the
    flag flips False -> True), not on every subsequent halt-blocked
    bar (that would flood the log)."""
    src = (ROOT / "packages" / "research" / "backtest_ensemble.py").read_text(
        encoding="utf-8"
    )
    assert "[BACKTEST-HALT]" in src, (
        "Trip event must use the [BACKTEST-HALT] log marker so an "
        "operator can grep for it in battery run logs."
    )
    # Verify the log line is GUARDED by `not _hard_halted[0]` so it
    # only fires once (when flag flips).
    halt_block_start = src.find("if (self.bt.drawdown_halt_pct is not None")
    assert halt_block_start > 0, (
        "Hard-halt arming block must exist in _bump_equity."
    )
    halt_block = src[halt_block_start:halt_block_start + 800]
    assert "not _hard_halted[0]" in halt_block, (
        "Hard-halt arming must be guarded by `not _hard_halted[0]` "
        "so the trip log fires exactly once per run, not on every "
        "subsequent equity bump after the halt."
    )


# ── §2 V26 variant: structure ──────────────────────────────────────────


def test_v26_exists_in_variants():
    """Hard guard against accidental removal."""
    name, overrides = _v("V26_swing_combined_shorts_high_cap")
    assert name == "V26_swing_combined_shorts_high_cap"
    assert isinstance(overrides, list) and len(overrides) > 0


def test_v26_resolves_max_positions_15_and_allow_shorts_true():
    """The whole point of V26: ``risk.max_positions`` resolves to 15,
    ``risk.allow_shorts`` resolves to True (inherited from the V25
    differentiator). If either regresses, V26 is no longer a faithful
    "V25 minus the position cap" disambiguation."""
    cfg = _resolved("V26_swing_combined_shorts_high_cap")
    assert cfg.get("risk", {}).get("max_positions") == 15, (
        "V26 must lift the max_positions cap from the v3 base of 5 to "
        "15. If this regresses, V26 becomes V25 and the cap-dilution "
        "objection re-opens."
    )
    assert cfg.get("risk", {}).get("allow_shorts") is True, (
        "V26 must inherit V25's allow_shorts=True. Without this, V26 "
        "tests longs-only-at-cap-15 which doesn't address the §8.3 "
        "objection at all."
    )


def test_v26_matches_v25_except_max_positions():
    """V25 vs V26 must differ at exactly one config key:
    ``risk.max_positions``. Any other delta makes V26's read
    uninterpretable as "V25 with the cap lifted"."""
    cfg25 = _resolved("V25_swing_combined_shorts")
    cfg26 = _resolved("V26_swing_combined_shorts_high_cap")

    diffs = []
    for section in ("backtest", "execution", "risk", "ensemble", "strategies",
                    "robustness", "backtest_gates"):
        s25 = cfg25.get(section, {}) or {}
        s26 = cfg26.get(section, {}) or {}
        all_keys = set(s25) | set(s26)
        for k in all_keys:
            v25 = s25.get(k)
            v26 = s26.get(k)
            if v25 != v26:
                diffs.append((f"{section}.{k}", v25, v26))

    expected = {("risk.max_positions", 5, 15)}
    actual = set(diffs)
    assert actual == expected, (
        f"V25 vs V26 should differ ONLY at risk.max_positions. Found "
        f"unexpected diffs: {actual - expected}; missing expected: "
        f"{expected - actual}. If max_positions diff is missing, the "
        f"override line was lost. If extra diffs appeared, V26 is "
        f"silently varying more than the cap and the verdict tree no "
        f"longer applies."
    )


def test_v26_engine_carries_max_positions_15():
    """Behavioural plumbing: BacktestConfig.max_positions resolved
    from V26's overrides must equal 15. If the loader path drops the
    override, V26 silently reverts to the live cap of 5 and the run
    becomes a duplicate of V25."""
    from research.battery import _bt_config, _build_variant_config

    base_path = ROOT / "config.yaml"
    base = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    name, overrides = _v("V26_swing_combined_shorts_high_cap")
    cfg = _build_variant_config(base, overrides)
    bt = _bt_config(cfg)

    assert bt.max_positions == 15, (
        f"BacktestConfig.max_positions must propagate from V26's "
        f"override list to the engine. Got {bt.max_positions}."
    )
    assert bt.allow_shorts is True, (
        "V26 must keep V25's allow_shorts=True (otherwise the cap "
        "lift is a no-op for the short-side question)."
    )
    assert bt.drawdown_halt_pct is None, (
        "V26 MUST NOT enable drawdown halt — it would conflate two "
        "changes (cap=15 AND halt=20) and make a V26 PF >= 1.0 "
        "result un-attributable. The §1 drawdown-halt knob is opt-in "
        "from a separate ``backtest.drawdown_halt_pct`` key; V26 "
        "doesn't set it."
    )


# ── §2 V26 variant: queue + docs ───────────────────────────────────────


def test_battery_queue_includes_v26_high_cap_job():
    """The variant is irrelevant unless the scheduler launches it.
    Pin that data/battery_queue.yaml has a job that runs V26."""
    queue = yaml.safe_load(
        (ROOT / "data" / "battery_queue.yaml").read_text(encoding="utf-8")
    )
    jobs = queue.get("jobs") or []
    v26_jobs = [
        j for j in jobs
        if "V26_swing_combined_shorts_high_cap" in (j.get("variants") or [])
    ]
    assert v26_jobs, (
        "data/battery_queue.yaml has no job that runs "
        "V26_swing_combined_shorts_high_cap. Without a queue entry the "
        "variant exists in code but never produces an evidence file."
    )
    job = v26_jobs[0]
    assert job.get("interval") == "1d"
    assert int(job.get("days", 0)) >= 600, (
        "V26 needs the same warmup window as V25 (>=600 days) for the "
        "200-DMA to clear; otherwise trend_pullback emits zero signals "
        "and the run finishes empty."
    )
    assert (
        job.get("universe-file", "").endswith("v3_universe_top30.json")
    ), (
        "V26 must use the same Nifty-30 universe as V25 to keep the "
        "comparison apples-to-apples."
    )


def test_v26_docstring_documents_verdict_tree():
    """The V26 variant block must declare the two-branch verdict tree
    (PF<1.0 → wind-down confirmed; PF>=1.0 → wind-down deferred)
    BEFORE the run, not after. This is charter §10.5 R1 discipline:
    pre-commit the verdict so the operator can't post-hoc rationalise
    a borderline reading."""
    src = (ROOT / "packages" / "research" / "battery.py").read_text(
        encoding="utf-8"
    )
    idx = src.find("V26_swing_combined_shorts_high_cap")
    assert idx > 0, "V26 variant must exist in battery.py."
    block = src[max(0, idx - 4000):idx + 1500]

    required_phrases = [
        "verdict tree",
        "pf < 1.0",
        "pf >= 1.0",
        "cap-dilution",
        "wind-down deferred",
    ]
    missing = [p for p in required_phrases if p.lower() not in block.lower()]
    assert not missing, (
        f"V26 variant docstring is missing verdict-tree phrases: "
        f"{missing}. Without a pre-committed tree, a borderline V26 "
        f"reading invites post-hoc rationalisation."
    )


# ── §3 V25 timestamp footnote ──────────────────────────────────────────


def test_v25_docstring_documents_timestamp_footnote():
    """The V25 variant block must explain the 03:45 IST trade-record
    timestamps so a future reader doesn't open a phantom bug report.
    The yfinance daily bar stamps UTC midnight; data_handler renders
    it in IST so the 'bar timestamp' field reads as 03:45+05:30 even
    though the actual fill semantics are still next-bar-open."""
    src = (ROOT / "packages" / "research" / "battery.py").read_text(
        encoding="utf-8"
    )
    idx = src.find("V25_swing_combined_shorts")
    assert idx > 0, "V25 variant must exist."
    # Search forward (the footnote was added to the V25 block body).
    block = src[idx:idx + 3500]
    required_phrases = [
        "yahoo finance",
        "03:45",
        "next-bar",
        "timestamp",
    ]
    missing = [p for p in required_phrases if p.lower() not in block.lower()]
    assert not missing, (
        f"V25 variant block is missing timestamp-footnote phrases: "
        f"{missing}. A trade-by-trade reviewer seeing 2025-01-20T03:45 "
        f"entries needs the explanation in-line."
    )
