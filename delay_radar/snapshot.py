"""Save and reload a raw static zip plus the realtime protobuf."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


def snapshot_root(data_dir: Path) -> Path:
    path = data_dir / "snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def record(data_dir: Path, static_path: Path, realtime: bytes, meta: dict) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = snapshot_root(data_dir) / stamp
    dest.mkdir()
    target = dest / "static.zip"
    try:
        os.link(static_path, target)
    except OSError:
        shutil.copy2(static_path, target)
    (dest / "realtime.pb").write_bytes(realtime)
    (dest / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return dest


def resolve(data_dir: Path, name: str) -> Path:
    root = snapshot_root(data_dir)
    if name == "latest":
        found = sorted(path for path in root.iterdir() if (path / "realtime.pb").exists())
        if not found:
            raise FileNotFoundError(f"no snapshots in {root}")
        return found[-1]
    dest = root / name
    if not (dest / "realtime.pb").exists():
        raise FileNotFoundError(f"snapshot {name} is not in {root}")
    return dest


def load(path: Path) -> tuple[Path, bytes, dict]:
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    return path / "static.zip", (path / "realtime.pb").read_bytes(), meta
