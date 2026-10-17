import subprocess
from pathlib import Path

BANNED = ["/backups", "/home/", "big-storage", "~/github", "/tmp/claude"]

# These files quote the banned strings as the rule itself.
EXCLUDED = {
    "tests/test_privacy.py",
    "docs/superpowers/specs/2026-10-17-bench-tooling-design.md",
    "docs/superpowers/plans/2026-10-17-bench-tooling.md",
}


def test_no_banned_strings_in_tracked_files():
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    hits = [
        (f, b) for f in files if f not in EXCLUDED for b in BANNED if b in Path(f).read_text(errors="ignore")
    ]
    assert hits == []
