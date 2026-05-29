"""Bug J regression tests for tools/cloud/bootstrap_backtester.sh.

Background
----------
docs/findings/findings_log_2026-05-27.md §1 documents Bug J: the bootstrap
script unconditionally chowned the entire ``/opt/trading-agent`` tree
to UID 1001 (the in-container ``trader`` user), which broke the
host-side ``battery-scheduler.service`` that runs as the SSH bootstrap
user (UID 1000 ``opc``/``ubuntu``). The scheduler crashed with
``PermissionError`` on its first ``data/battery_queue_state.json.tmp``
write because UID 1000 has no write permission on a 1001-owned tree.

The fix (commit referenced in §10 of findings_log_2026-05-27.md) splits
ownership three ways:

* ``data/`` and ``logs/battery_scheduler/`` -> ``$USER:$USER`` (host)
* ``data/research/`` and ``logs/backtests/`` -> ``1001:1001`` (container)
* ``models/`` -> ``1001:1001`` (read-only by both, container default)

These tests pin that contract so a future refactor of the bootstrap
script can't re-introduce Bug J without a CI failure.

Why these tests are file-text assertions and not integration tests:
the bootstrap script runs over SSH against a remote VM. Truly
integration-testing it would require a disposable VM. Pinning the
chown commands as text is the cheapest possible regression guard --
the entire failure mode of Bug J was a single line of bash, and a
single line of bash is what we're asserting here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "cloud" / "bootstrap_backtester.sh"


@pytest.fixture(scope="module")
def script_text() -> str:
    if not SCRIPT.exists():
        pytest.fail(f"bootstrap_backtester.sh missing at {SCRIPT}")
    return SCRIPT.read_text(encoding="utf-8")


def test_no_blanket_1001_chown_of_full_tree(script_text: str) -> None:
    """Bug J regression: the broken pre-fix line chowned the entire
    logs/data/models tree to 1001:1001 in one shot. That single command
    is what created the permission collision with the host scheduler.
    """
    forbidden = (
        "sudo chown -R 1001:1001 ${TRADER_HOME}/logs ${TRADER_HOME}/data ${TRADER_HOME}/models"
    )
    assert forbidden not in script_text, (
        "Bug J regression: bootstrap_backtester.sh contains the broken pre-fix "
        "line that chowns logs+data+models to 1001:1001 in one shot. The host-"
        "side battery-scheduler.service will crash with PermissionError on its "
        "first data/battery_queue_state.json.tmp write. See "
        "docs/findings/findings_log_2026-05-27.md §1 for the root cause and the "
        "three-way ownership split that fixes it."
    )


def test_data_root_is_host_owned(script_text: str) -> None:
    """data/ holds the scheduler's queue checkpoint -- must be host-owned."""
    # The chown line for host-owned paths must include data/ and use $USER.
    assert "chown -R \\$USER:\\$USER ${TRADER_HOME}/data" in script_text or \
           "chown -R \\$USER:\\$USER ${TRADER_HOME}/data " in script_text or \
           "${TRADER_HOME}/data \\\n" in script_text, (
        "data/ should be chown'd to \\$USER (host), not 1001 (container). "
        "The scheduler is a host process and writes data/battery_queue_state.json."
    )


def test_battery_scheduler_log_dir_is_host_owned(script_text: str) -> None:
    """logs/battery_scheduler/ holds the scheduler's operator log -- host-owned."""
    assert "${TRADER_HOME}/logs/battery_scheduler" in script_text, (
        "logs/battery_scheduler/ must be explicitly created and chown'd to "
        "the host \\$USER. Without this, journalctl gets all the scheduler "
        "logs and the host-side log file is silently un-writable."
    )


def test_logs_backtests_is_container_owned(script_text: str) -> None:
    """logs/backtests/<run_id>/ is written by container workers -- 1001-owned."""
    assert "chown -R 1001:1001" in script_text, (
        "Container-side workers (UID 1001) must have explicit ownership of "
        "logs/backtests/. Without this, mkdir logs/backtests/<run_id> fails "
        "as the first action of every battery worker."
    )
    # Verify the 1001 chown line includes the right paths
    one_oh_one_block = _extract_chown_block(script_text, owner="1001:1001")
    assert "logs/backtests" in one_oh_one_block, (
        f"1001:1001 chown block must include logs/backtests. Got:\n{one_oh_one_block}"
    )
    assert "data/research" in one_oh_one_block, (
        f"1001:1001 chown block must include data/research/. Got:\n{one_oh_one_block}"
    )


def test_models_dir_chowned_for_container_read(script_text: str) -> None:
    """models/ is bind-mounted read-only into the container; ownership only
    needs to be readable by 1001. We default to 1001:1001 since the container
    is the only consumer that actually accesses it."""
    one_oh_one_block = _extract_chown_block(script_text, owner="1001:1001")
    assert "models" in one_oh_one_block, (
        "models/ should be chown'd to 1001:1001 for container read access."
    )


def test_writer_probes_present(script_text: str) -> None:
    """Self-verification: the bootstrap must exercise BOTH writers before
    declaring success. Without these probes Bug J could slip through again
    if someone refactors the chown logic and breaks one of the writer
    code paths.
    """
    # Host-side probe: writes + removes a file under data/ as the bootstrap user.
    assert "data/.bug_j_probe_host" in script_text, (
        "Bootstrap should include a host-side write probe under data/ "
        "(touch + rm of a marker file). See step [7/8]."
    )
    # Container-side probe: writes + removes a file under logs/backtests/ via docker run.
    assert "logs/backtests/.bug_j_probe" in script_text, (
        "Bootstrap should include a container-side write probe under "
        "logs/backtests/ (docker run mkdir + rmdir). See step [8/8]."
    )


def test_bug_j_documented_in_script(script_text: str) -> None:
    """A future maintainer must be able to find the link to the findings
    doc straight from the script -- no archaeology required."""
    assert "Bug J" in script_text, (
        "The bootstrap script must reference 'Bug J' so a future "
        "maintainer can grep their way to the root cause."
    )
    assert "findings_log_2026-05-27.md" in script_text, (
        "The bootstrap script must link to the findings_log file that "
        "documents the permission split and root cause."
    )


# ────────────────────────── helpers ──────────────────────────


def _extract_chown_block(script_text: str, owner: str) -> str:
    """Return the (possibly multi-line) chown command for the given owner.

    The script formats long chown commands with line continuations:

        sudo chown -R 1001:1001 ${TRADER_HOME}/data/research \\
                                ${TRADER_HOME}/logs/backtests \\
                                ${TRADER_HOME}/models

    We need to collect all continuation lines until we hit one that
    doesn't end in backslash. A naive substring match would miss the
    paths on continuation lines.
    """
    # Match the executed command (``sudo chown ...``), NOT the comment-block
    # discussion of the pre-fix behaviour. Without the ``sudo`` prefix the
    # helper would happily match e.g. "Pre-fix this script did 'chown -R
    # 1001:1001 logs data models'", which is just narrative text.
    needle = f"sudo chown -R {owner}"
    start = script_text.find(needle)
    if start == -1:
        return ""
    block_lines: list[str] = []
    pos = start
    while pos < len(script_text):
        eol = script_text.find("\n", pos)
        if eol == -1:
            block_lines.append(script_text[pos:])
            break
        line = script_text[pos:eol]
        block_lines.append(line)
        if not line.rstrip().endswith("\\"):
            break
        pos = eol + 1
    return "\n".join(block_lines)
