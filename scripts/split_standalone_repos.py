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
SKIP_DIRS = {"__pycache__", "bin", "obj", "build", "dist", "target", "node_modules", ".git", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".exe", ".dll", ".pdb", ".db", ".db-wal", ".db-shm", ".lock", ".tgz", ".nupkg", ".snupkg", ".crate"}


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


STANDALONE_GITIGNORE = """# Build & runtime artifacts
bin/
obj/
build/
dist/
node_modules/
__pycache__/
*.egg-info/
*.pyc
*.pyo
*.exe
*.dll
*.pdb
*.db
*.db-wal
*.db-shm
"""


def export_standalone_repos(out_root: Path, github_org: str = "aiaimimi0920") -> list[Path]:
    out_root = out_root.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    exported: list[Path] = []

    for name in REPOS:
        src = ROOT / name
        dst = out_root / name
        if dst.exists():
            shutil.rmtree(dst)
        copy_clean_tree(src, dst)
        gitignore_path = dst / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text(STANDALONE_GITIGNORE, encoding="utf-8", newline="\n")
        subprocess.run(["git", "init", "-b", "main"], cwd=str(dst), check=True, stdout=subprocess.DEVNULL)
        subprocess.run(
            ["git", "remote", "add", "origin", f"https://github.com/{github_org}/{name}.git"],
            cwd=str(dst),
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(["git", "add", "-A"], cwd=str(dst), check=True, stdout=subprocess.DEVNULL)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=G2A Release Bot",
                "-c",
                "user.email=noreply@g2a.local",
                "commit",
                "-m",
                f"feat: release {name} V0.0.1 standalone repository",
            ],
            cwd=str(dst),
            check=True,
            stdout=subprocess.DEVNULL,
        )
        exported.append(dst)
    return exported


def main() -> None:
    parser = argparse.ArgumentParser(description="Split G2A V0.0.1 three apps into standalone Git repositories")
    parser.add_argument("--output-dir", required=True, help="Destination parent directory for exported repos")
    parser.add_argument("--github-org", default="aiaimimi0920")
    args = parser.parse_args()

    for dst in export_standalone_repos(Path(args.output_dir), github_org=args.github_org):
        print(f"Exported standalone Git repo: {dst}")


if __name__ == "__main__":
    main()
