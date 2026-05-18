"""Regression guard: every importable module under ``packages/`` must import
cleanly on Python 3.11 — the version the production container runs.

Why this test exists
====================

On 2026-05-18 the production trader was found in a 20-hour crash loop with:

    ERROR | Agent crashed (#N): name 'Dict' is not defined

Root cause: ``packages/core/execution.py`` used ``Dict[str, Dict]`` in the
signature of ``reconcile_positions_with_broker`` (P2 restart-cluster work,
2026-05-17) but only imported ``Optional`` from ``typing``. Without
``from __future__ import annotations``, Python 3.11 evaluates function-
signature annotations at *definition time* — i.e. at module import time —
so the missing ``Dict`` raised ``NameError`` and aborted the daemon boot.

The local dev machine runs Python 3.14, where PEP 649 makes annotation
evaluation lazy by default. That hid the bug entirely from the local test
suite (996/996 green) and from every direct ``python -c`` check on the
developer's machine. Only the container — pinned to ``python:3.11-slim``
in the Dockerfile — exposed it, and only after the deploy went live.

What this test catches
======================

It walks every ``*.py`` file under ``packages/`` and ``importlib.import_module``s
it. Any of the following will fail this test:

  * ``NameError`` from a missing ``from typing import Foo`` while a function
    or variable annotation uses ``Foo[...]`` (the exact bug above).
  * Any other module-level evaluation error introduced by a careless edit
    (e.g. a missing project import, a renamed module, a circular import).

Two test cases, run on every package module:

  1. ``test_module_imports_cleanly``: ``importlib.import_module(name)``.
     Catches the eager-eval form (Python 3.11 container behaviour).

  2. ``test_module_annotations_resolve``: walks every function / class
     in the imported module and calls ``typing.get_type_hints(obj)``.
     This forces annotation evaluation EVEN ON Python 3.14+ where
     PEP 649 makes evaluation lazy by default. Without this second
     case the eager-eval bug remains undetectable on the developer's
     own machine — exactly the failure mode that put us in the
     2026-05-18 crash loop in the first place.

What it does NOT catch
======================

  * Errors that only manifest mid-function (e.g. a missing import only
    used inside a code path that this test never executes).

For everything else the import + get_type_hints sweep is enough.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import typing
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGES_DIR = PROJECT_ROOT / "packages"

# These submodules import optional / heavy 3rd-party deps that are not
# guaranteed to be installed in every test environment (e.g. CI without
# GPU PyTorch). They are still imported by the agent at runtime, so we
# attempt them but tolerate ``ImportError`` / ``ModuleNotFoundError``.
# A ``NameError`` from a missing typing import would NOT be silenced
# here — it surfaces as a test failure.
OPTIONAL_IMPORT_ALLOWED = (
    ImportError,
    ModuleNotFoundError,
)


def _iter_package_modules() -> list[str]:
    """Yield every ``packages.<subpkg>.<module>`` dotted name we can find."""
    names: list[str] = []
    for finder, modname, ispkg in pkgutil.walk_packages(
        path=[str(PACKAGES_DIR)],
        prefix="",
    ):
        if ispkg:
            continue
        names.append(modname)
    return sorted(names)


_MODULES = _iter_package_modules()


@pytest.mark.parametrize("module_name", _MODULES)
def test_module_imports_cleanly(module_name: str) -> None:
    """Importing the module must not raise NameError / SyntaxError / AttributeError.

    Optional-dependency ImportError is tolerated (see OPTIONAL_IMPORT_ALLOWED);
    everything else fails the test loudly with the offending module name.
    """
    try:
        importlib.import_module(module_name)
    except OPTIONAL_IMPORT_ALLOWED as exc:
        pytest.skip(
            f"{module_name} skipped due to missing optional dependency: "
            f"{type(exc).__name__}: {exc}"
        )
    except NameError as exc:
        pytest.fail(
            f"{module_name} raised NameError at import time: {exc}. "
            "Almost certainly a missing 'from typing import X' or a "
            "stale identifier in a module-level annotation. The local "
            "dev Python (3.14, PEP 649 lazy annotations) hides this; "
            "the container (3.11, eager annotations) exposes it. "
            "Add the missing typing import OR 'from __future__ import "
            "annotations'."
        )
    except Exception as exc:  # noqa: BLE001 - we want the bare type + msg
        pytest.fail(
            f"{module_name} raised {type(exc).__name__} at import time: {exc}"
        )


@pytest.mark.parametrize("module_name", _MODULES)
def test_module_annotations_resolve(module_name: str) -> None:
    """``typing.get_type_hints`` on every top-level function/class in the
    module must succeed.

    This forces annotation evaluation even under Python 3.14's PEP 649
    lazy semantics, so a missing ``from typing import Foo`` shows up
    locally before it ships to the 3.11 container.

    Heuristics:
      * Only inspect objects DEFINED IN this module (skip re-exports).
      * Skip private (``_``-prefixed) names — they are implementation
        detail and not part of the supported surface.
      * Skip dataclasses' synthesised ``__init__`` (their type hints
        come straight from the class body; ``get_type_hints`` on the
        class itself already covers them).
    """
    try:
        module = importlib.import_module(module_name)
    except OPTIONAL_IMPORT_ALLOWED:
        pytest.skip(f"{module_name} skipped due to missing optional dependency")

    failures: list[str] = []

    def _check(qualname: str, obj: object) -> None:
        try:
            typing.get_type_hints(obj)
        except NameError as exc:
            failures.append(
                f"  {qualname}: NameError: {exc} "
                "(missing 'from typing import X' or stale identifier in an annotation)"
            )
        except (AttributeError, TypeError):
            # AttributeError: some descriptors don't expose __annotations__.
            # TypeError: e.g. C-extension classes typing can't introspect.
            return
        except Exception:  # noqa: BLE001 - non-NameError = unrelated, ignore
            return

    for name, obj in inspect.getmembers(module):
        if name.startswith("_"):
            continue
        obj_module = getattr(obj, "__module__", None)
        if obj_module != module_name:
            continue

        if inspect.isfunction(obj) or inspect.ismethod(obj):
            _check(f"{module_name}.{name}", obj)
            continue

        if inspect.isclass(obj):
            _check(f"{module_name}.{name}", obj)
            # Also walk methods defined IN THIS CLASS (not inherited from
            # bases that live in a different module — those are someone
            # else's contract to type-check). This is the layer that
            # actually catches the 2026-05-18 NameError, because the
            # offender (reconcile_positions_with_broker) is a method on
            # ExecutionEngine, not a top-level function.
            for m_name, m_obj in inspect.getmembers(obj):
                if m_name.startswith("_"):
                    continue
                if not (inspect.isfunction(m_obj) or inspect.ismethod(m_obj)):
                    continue
                if getattr(m_obj, "__module__", None) != module_name:
                    continue
                _check(f"{module_name}.{name}.{m_name}", m_obj)

    if failures:
        pytest.fail(
            f"typing.get_type_hints() failed for {len(failures)} object(s) in "
            f"{module_name}:\n" + "\n".join(failures)
        )


def test_module_inventory_is_nonempty() -> None:
    """Sanity guard: the walker should find at least a few dozen modules.
    A drop to zero would mean ``PACKAGES_DIR`` got mis-rooted and the
    parametrized test above silently runs zero cases — which would pass
    while protecting nothing.
    """
    assert len(_MODULES) >= 30, (
        f"expected >=30 modules under packages/, found {len(_MODULES)}: {_MODULES}"
    )
