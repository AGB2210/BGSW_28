"""Feature-flag writes for faultctl and the Action API.

flagd watches the demo's flag file and the demo's flag UI edits the same file,
so changing a flag's `defaultVariant` here is the same operation the UI does.
The agent never reads this file.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

DEMO_DIR = Path(os.environ.get("DEMO_DIR", Path.home() / "opentelemetry-demo"))
FLAG_FILE = DEMO_DIR / "src" / "flagd" / "demo.flagd.json"


class FlagError(ValueError):
    pass


def _load() -> dict:
    return json.loads(FLAG_FILE.read_text())


def set_variant(flag: str, variant: str) -> str:
    """Set a flag's default variant and return the previous one."""
    data = _load()
    spec = data["flags"].get(flag)
    if spec is None:
        raise FlagError(f"unknown flag {flag!r}")
    if variant not in spec["variants"]:
        raise FlagError(f"{flag} has no variant {variant!r}; options: {sorted(spec['variants'])}")
    previous = spec["defaultVariant"]
    spec["defaultVariant"] = variant
    # Write in place (not rename) so flagd's file watch on the bind mount keeps working.
    with FLAG_FILE.open("w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return previous


def backup(dest: Path) -> None:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FLAG_FILE, dest)


def restore(src: Path) -> None:
    FLAG_FILE.write_text(src.read_text())
