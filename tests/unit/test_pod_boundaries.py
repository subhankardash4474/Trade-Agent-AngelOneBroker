"""
Pod-boundary enforcement tests.

After Phase 1 has moved everything under packages/, these tests prevent
accidental cross-pod imports that would couple deployable units. Run as part
of the unit suite; they are fast (pure AST scan, no execution).

Skip-marked until Phase 1 has actually executed (i.e. until packages/core/
exists). The check `_phase1_done()` flips them on automatically as soon as
the move script lands.

Rules enforced:
  core         imports allowed: stdlib + third-party only (no pod siblings)
  strategies   imports allowed: stdlib + third-party + core
  brokers      imports allowed: stdlib + third-party + core
  monitoring   imports allowed: stdlib + third-party + core
  training     imports allowed: stdlib + third-party + core + strategies
  trader       imports allowed: stdlib + third-party + core + strategies + brokers + monitoring
  research     imports allowed: stdlib + third-party + core + strategies + training + monitoring
                                 (NOT trader, NOT ui)
  ui           imports allowed: stdlib + third-party + core
                                 (NOT trader, NOT research, NOT strategies)

If any module in a pod imports a forbidden sibling, this test fails with the
exact (file, line, statement) so it can be fixed in seconds.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "packages"

ALLOWED_IMPORTS: dict[str, set[str]] = {
    "core":       set(),  # no pod-sibling imports
    "strategies": {"core"},
    "brokers":    {"core"},
    "monitoring": {"core"},
    "training":   {"core", "strategies"},
    "trader":     {"core", "strategies", "brokers", "monitoring"},
    "research":   {"core", "strategies", "training", "monitoring"},
    "ui":         {"core"},
}

# Modules that exist as top-level imports because they are siblings under
# packages/. We only care about THESE — third-party imports (pandas, etc.)
# are not pods so they are always allowed.
SIBLING_MODULES = set(ALLOWED_IMPORTS)


def _phase1_done() -> bool:
    """Phase 1 has executed once packages/core/ exists with at least one .py."""
    return (PKG / "core").is_dir() and any((PKG / "core").glob("*.py"))


pytestmark = pytest.mark.skipif(
    not _phase1_done(),
    reason="Phase 1 (packages/<pod>/ population) has not executed yet -- "
           "this test will activate automatically once it has.",
)


def _imports_in_file(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, top-level module)] for every import in `path`."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                top = n.name.split(".")[0]
                out.append((node.lineno, top))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                top = node.module.split(".")[0]
                out.append((node.lineno, top))
    return out


def _violations_for_pod(pod: str) -> list[tuple[Path, int, str]]:
    """Return [(file, lineno, illegal_import)] for `pod`."""
    pod_dir = PKG / pod
    if not pod_dir.is_dir():
        return []
    allowed = ALLOWED_IMPORTS[pod]
    bad: list[tuple[Path, int, str]] = []
    for py in pod_dir.rglob("*.py"):
        for ln, mod in _imports_in_file(py):
            if mod == pod:
                continue  # intra-pod always fine
            if mod in SIBLING_MODULES and mod not in allowed:
                bad.append((py, ln, mod))
    return bad


@pytest.mark.parametrize("pod", sorted(ALLOWED_IMPORTS.keys()))
def test_pod_does_not_import_forbidden_siblings(pod: str) -> None:
    bad = _violations_for_pod(pod)
    if bad:
        msg_lines = [f"\n{pod} has {len(bad)} forbidden cross-pod import(s):"]
        for f, ln, mod in bad:
            rel = f.relative_to(ROOT).as_posix()
            msg_lines.append(f"  {rel}:{ln}  imports `{mod}` (allowed: {sorted(ALLOWED_IMPORTS[pod])})")
        pytest.fail("\n".join(msg_lines))


def test_packages_root_exists() -> None:
    """Sanity: the new layout is in place."""
    assert PKG.is_dir(), "packages/ directory missing"
    for pod in ALLOWED_IMPORTS:
        assert (PKG / pod).is_dir(), f"packages/{pod}/ missing -- Phase 1 incomplete"


def test_sys_path_bootstrap_exists() -> None:
    """The conftest.py at project root must add packages/ to sys.path."""
    conftest = ROOT / "conftest.py"
    assert conftest.exists(), "project-root conftest.py missing -- packages won't be importable"
    text = conftest.read_text(encoding="utf-8")
    assert "packages" in text and "sys.path" in text, \
        "project-root conftest.py exists but doesn't bootstrap packages/ on sys.path"
