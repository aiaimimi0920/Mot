"""
Exports `interaction-origin`, `intelligent-hub`, and `computation-core` from the parent `Mot`
monorepo into three clean, standalone Git repositories ready for independent publishing.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPOS = ("interaction-origin", "intelligent-hub", "computation-core")
SKIP_DIRS = {"__pycache__", "bin", "obj", "build", "dist", "node_modules", ".git", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".exe", ".dll", ".pdb", ".db", ".db-wal", ".db-shm"}


def copy_clean_tree(src: Path, dst: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        rel_dir = Path(dirpath).relative_to(src)
        target_dir = dst / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for fn in filenames:
            if any(fn.endswith(sfx) for sfx in SKIP_SUFFIXES):
                continue
            s_file = Path(dirpath) / fn
            d_file = target_dir / fn
            shutil.copy2(s_file, d_file)
            if fn == "README.md":
                txt = d_file.read_text(encoding="utf-8")
                txt = txt.replace(
                    "../G2A/docs/standard/",
                    "https://github.com/aiaimimi0920/G2A/blob/main/docs/standard/",
                )
                d_file.write_text(txt, encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Split G2A V0.0.1 three apps into standalone Git repositories")
    parser.add_argument("--output-dir", required=True, help="Destination parent directory for exported repos")
    parser.add_argument("--github-org", default="aiaimimi0920")
    args = parser.parse_args()

    out_root = Path(args.output_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    for name in REPOS:
        src = ROOT / name
        dst = out_root / name
        if dst.exists():
            shutil.rmtree(dst)
        copy_clean_tree(src, dst)
        subprocess.run(["git", "init", "-b", "main"], cwd=str(dst), check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", f"https://github.com/{args.github_org}/{name}.git"],
            cwd=str(dst),
            check=True,
        )
        subprocess.run(["git", "add", "-A"], cwd=str(dst), check=True)
        subprocess.run(
            ["git", "commit", "-m", f"feat: release {name} V0.0.1 standalone repository"],
            cwd=str(dst),
            check=True,
        )
        print(f"Exported standalone Git repo: {dst}")


if __name__ == "__main__":
    main()
