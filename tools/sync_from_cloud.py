"""
sync_from_cloud.py — pull diagnostics, backtests, and proposals from the
research pod's storage into a local mirror so Cursor (and you) can analyze
them in this folder.

Source can be:
  - file:///path/to/local/mirror   (dev mode -- before we move to AWS)
  - s3://bucket-name               (prod mode -- after Phase 2)

Local destination is always `logs/cloud_sync/` (gitignored).

Usage:
  python tools/sync_from_cloud.py                          # default source, all
  python tools/sync_from_cloud.py --since 7d               # last week only
  python tools/sync_from_cloud.py --source file:///D:/cloud_mirror
  python tools/sync_from_cloud.py --source s3://my-bucket  # prod
  python tools/sync_from_cloud.py --kind diagnostics       # just diag
  python tools/sync_from_cloud.py --dry-run                # show, don't copy

Default source resolution (first hit wins):
  1. --source CLI flag
  2. CLOUD_SYNC_SOURCE environment variable
  3. data/local_cloud_mirror/   (so the dev workflow works out of the box)

This is a STUB that makes the workflow real today. The S3 driver path is
implemented in code but lazy-imports boto3, so it does not require AWS SDK
to be installed for local dev.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DEFAULT_LOCAL_MIRROR = ROOT / "data" / "local_cloud_mirror"
DEST_ROOT = ROOT / "logs" / "cloud_sync"

# What we expect the cloud pod to write. Order matters for display, not logic.
KNOWN_KINDS = (
    "diagnostics",
    "backtests",
    "proposals",
    "trades-export",
)


# ─────────────────────────────────────────────────────────────────────
# Source drivers
# ─────────────────────────────────────────────────────────────────────
class SyncSource:
    """Abstract: a source we can list-then-download from."""

    def list(self, prefix: str) -> Iterator[tuple[str, datetime]]:
        """Yield (relative_key, last_modified_utc) tuples under `prefix`."""
        raise NotImplementedError

    def fetch(self, key: str, dest: Path) -> int:
        """Copy `key` to `dest`. Return bytes written."""
        raise NotImplementedError


class FileSource(SyncSource):
    """file:// driver — used for local dev and for staging before S3 cutover."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)

    def list(self, prefix: str) -> Iterator[tuple[str, datetime]]:
        base = self.root / prefix
        if not base.exists():
            return
        for p in base.rglob("*"):
            if p.is_file():
                rel = p.relative_to(self.root).as_posix()
                yield rel, datetime.fromtimestamp(p.stat().st_mtime)

    def fetch(self, key: str, dest: Path) -> int:
        src = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return dest.stat().st_size


class S3Source(SyncSource):
    """s3:// driver — lazy-imports boto3 so dev doesn't need AWS SDK installed."""

    def __init__(self, bucket: str, prefix: str = ""):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        try:
            import boto3  # noqa: F401
        except ImportError:
            print("[ERROR] boto3 not installed. Either pip install boto3, or "
                  "use a file:// source for local dev.")
            sys.exit(2)
        import boto3
        self._s3 = boto3.client("s3")

    def list(self, key_prefix: str) -> Iterator[tuple[str, datetime]]:
        full_prefix = f"{self.prefix}/{key_prefix}".strip("/")
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []) or []:
                rel = obj["Key"][len(self.prefix) + 1:] if self.prefix else obj["Key"]
                yield rel, obj["LastModified"].replace(tzinfo=None)

    def fetch(self, key: str, dest: Path) -> int:
        full_key = f"{self.prefix}/{key}".strip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._s3.download_file(self.bucket, full_key, str(dest))
        return dest.stat().st_size


def make_source(uri: str) -> SyncSource:
    """Parse a source URI and return the matching driver."""
    parsed = urlparse(uri)
    if parsed.scheme in ("file", "") and (parsed.path or uri):
        # support both 'file:///D:/foo' and bare paths
        path_str = parsed.path if parsed.scheme == "file" else uri
        if os.name == "nt" and path_str.startswith("/") and len(path_str) > 2 and path_str[2] == ":":
            path_str = path_str.lstrip("/")  # strip leading / before drive letter on Windows
        return FileSource(Path(path_str))
    if parsed.scheme == "s3":
        bucket = parsed.netloc
        prefix = parsed.path.strip("/")
        return S3Source(bucket, prefix)
    raise ValueError(f"Unsupported source URI scheme: {parsed.scheme!r} (expected file:// or s3://)")


# ─────────────────────────────────────────────────────────────────────
# Time filter
# ─────────────────────────────────────────────────────────────────────
_DURATION_RX = re.compile(r"^(\d+)\s*([dhwm])$")


def parse_since(spec: str | None) -> datetime | None:
    """Parse '7d', '24h', '2w', '1m' (months ~30d) into a UTC cutoff datetime."""
    if not spec:
        return None
    m = _DURATION_RX.match(spec.strip().lower())
    if not m:
        raise ValueError(f"--since must look like '7d', '24h', '2w', '1m' (got {spec!r})")
    n, unit = int(m.group(1)), m.group(2)
    delta = {
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
        "m": timedelta(days=30 * n),
    }[unit]
    return datetime.now(timezone.utc).replace(tzinfo=None) - delta


# ─────────────────────────────────────────────────────────────────────
# Sync engine
# ─────────────────────────────────────────────────────────────────────
def sync(source: SyncSource, kinds: Iterable[str], since: datetime | None,
         dry_run: bool = False) -> dict:
    """Returns summary stats per kind."""
    summary: dict = {}
    for kind in kinds:
        copied = 0
        skipped_old = 0
        skipped_uptodate = 0
        bytes_total = 0
        files = list(source.list(kind))
        if since:
            files = [(k, ts) for (k, ts) in files if ts >= since]
        for key, ts in files:
            dest = DEST_ROOT / key
            if dest.exists() and dest.stat().st_mtime >= ts.timestamp():
                skipped_uptodate += 1
                continue
            if dry_run:
                print(f"  [DRY] would copy {key}  ({ts:%Y-%m-%d %H:%M})")
                copied += 1
                continue
            try:
                bytes_total += source.fetch(key, dest)
                # preserve mtime so future syncs can dedupe
                os.utime(dest, (ts.timestamp(), ts.timestamp()))
                copied += 1
            except Exception as e:
                print(f"  [WARN] failed {key}: {e}")
        summary[kind] = {
            "copied": copied,
            "uptodate": skipped_uptodate,
            "old": skipped_old,
            "bytes": bytes_total,
        }
    return summary


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Sync research-pod outputs (diagnostics, backtests, proposals) "
                    "from cloud or local mirror into logs/cloud_sync/."
    )
    ap.add_argument("--source", default=None,
                    help="Source URI: file:///path or s3://bucket/prefix. "
                         "Defaults to $CLOUD_SYNC_SOURCE or data/local_cloud_mirror/.")
    ap.add_argument("--since", default=None,
                    help="Pull only items newer than this. Format: '7d', '24h', '2w', '1m'.")
    ap.add_argument("--kind", default=None, choices=KNOWN_KINDS,
                    help="Sync only one category (default: all).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be copied; don't write anything.")
    args = ap.parse_args()

    # Resolve source
    source_uri = (
        args.source
        or os.environ.get("CLOUD_SYNC_SOURCE")
        or f"file://{DEFAULT_LOCAL_MIRROR.resolve().as_posix()}"
    )
    print(f"[sync] source = {source_uri}")
    print(f"[sync] dest   = {DEST_ROOT.relative_to(ROOT)}/")
    if args.since:
        print(f"[sync] since  = {args.since}")
    if args.dry_run:
        print("[sync] DRY RUN -- no files will be written.")
    print()

    try:
        source = make_source(source_uri)
    except Exception as e:
        print(f"[ERROR] could not open source: {e}")
        return 2

    since_dt = parse_since(args.since)
    kinds = [args.kind] if args.kind else list(KNOWN_KINDS)

    DEST_ROOT.mkdir(parents=True, exist_ok=True)
    summary = sync(source, kinds, since_dt, dry_run=args.dry_run)

    # Report
    total_copied = sum(s["copied"] for s in summary.values())
    total_uptodate = sum(s["uptodate"] for s in summary.values())
    total_bytes = sum(s["bytes"] for s in summary.values())
    print()
    print("=" * 58)
    for kind in kinds:
        s = summary[kind]
        print(f"  {kind:14s}  copied={s['copied']:4d}  uptodate={s['uptodate']:4d}  "
              f"bytes={s['bytes']:>10,}")
    print("=" * 58)
    print(f"  TOTAL          copied={total_copied:4d}  uptodate={total_uptodate:4d}  "
          f"bytes={total_bytes:>10,}")
    print()

    if total_copied == 0 and total_uptodate == 0:
        print("[i] No files found at source. If this is dev mode, the research pod "
              "hasn't written anything yet -- that's expected before Phase 1.")
        return 0
    print(f"[OK] Cloud sync complete -> {DEST_ROOT.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
