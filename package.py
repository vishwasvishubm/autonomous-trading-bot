"""Build the submission ZIP without secrets.

    python package.py            # -> dist/manthan-bot-YYYYMMDD-HHMM.zip

Excludes .env (the real token), logs, recorded streams, caches and kill-switch
files, then scans every file that would be shipped for anything that looks like a
credential. If the scan finds something, no ZIP is written.
"""
from __future__ import annotations

import os
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXCLUDE_DIRS = {".git", "__pycache__", "logs", "dist", ".venv", "venv", ".pytest_cache", ".idea", ".vscode",
                "stream", "reports_private"}
EXCLUDE_NAMES = {".env", "STOP", "FLATTEN", "state.json"}
EXCLUDE_SUFFIXES = {".pyc", ".log", ".zip", ".tmp", ".jsonl"}

PATTERNS = [
    ("JWT-like token", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("filled-in BOT_ACCESS_TOKEN", re.compile(r"^[ \t]*BOT_ACCESS_TOKEN[ \t]*=[ \t]*[^\s#]{8,}", re.M)),
    ("Bearer credential", re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}")),
    ("password assignment", re.compile(r"(?i)\bpass(word)?\s*[:=]\s*['\"]?[^\s'\"#]{4,}")),
]
ALLOW = re.compile(r"Bearer \{|f\"Bearer|Bearer <|password\s*[:=]\s*['\"]?(\.\.\.|<|\$\{)")


def collect(root: Path = ROOT) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if name in EXCLUDE_NAMES or path.suffix in EXCLUDE_SUFFIXES:
                continue
            if name.startswith(".env") and name != ".env.example":
                continue
            out.append(rel / name)
    return sorted(out)


def scan(files: list[Path], root: Path = ROOT, extra_secrets: list[str] | None = None) -> list[str]:
    """Return findings as 'file: kind' (never the secret itself)."""
    secrets = [s for s in (extra_secrets or []) if s and len(s) >= 12]
    findings = []
    for rel in files:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for kind, rx in PATTERNS:
            for m in rx.finditer(text):
                line = text[text.rfind("\n", 0, m.start()) + 1: text.find("\n", m.end())]
                if not ALLOW.search(line):
                    findings.append(f"{rel}: {kind}")
                    break
        for s in secrets:
            if s in text:
                findings.append(f"{rel}: contains the live BOT_ACCESS_TOKEN value")
    return sorted(set(findings))


def live_secrets() -> list[str]:
    vals = [os.environ.get("BOT_ACCESS_TOKEN", "")]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("BOT_ACCESS_TOKEN="):
                vals.append(line.split("=", 1)[1].strip())
    return [v for v in vals if v]


def main() -> int:
    files = collect()
    findings = scan(files, extra_secrets=live_secrets())
    if findings:
        print("REFUSING to package: possible secrets found:")
        for f in findings:
            print("  -", f)
        return 1
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / f"manthan-bot-{datetime.now():%Y%m%d-%H%M}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            z.write(ROOT / rel, Path("manthan-bot") / rel)
    print(f"wrote {out} ({len(files)} files, secret scan clean, .env excluded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
