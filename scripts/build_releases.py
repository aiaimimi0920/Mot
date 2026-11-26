"""
Cross-platform release packager for G2A V0.0.1 three standalone products:
  1. intelligent-hub: standalone binaries for windows/amd64, linux/amd64, darwin/arm64;
  2. interaction-origin: Godot 4 addon, C++17 headers, Unreal Engine 5 Plugin, Rust crate,
     C# .NET/Unity SDK, Unity UPM package, TypeScript/ES2022 SDK, Python SDK;
  3. computation-core: Python runtime/SDK, TypeScript/ES2022 CoreSDK, Go CoreSDK, C# .NET 8 CoreSDK;
Outputs 15 zip archives + SHA256SUMS.txt into `release-artifacts/v0.0.1/`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "release-artifacts" / "v0.0.1"

SKIP_DIRS = {"__pycache__", "bin", "obj", "build", "dist", "target", "node_modules", ".git", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".exe", ".dll", ".pdb", ".db", ".db-wal", ".db-shm", ".lock"}


def add_tree_to_zip(zf: zipfile.ZipFile, src_dir: Path, arc_prefix: str) -> None:
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames[:] = [d for d in sorted(dirnames) if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for fn in sorted(filenames):
            if any(fn.endswith(sfx) for sfx in SKIP_SUFFIXES):
                continue
            full_path = Path(dirpath) / fn
            rel = full_path.relative_to(src_dir).as_posix()
            arcname = f"{arc_prefix}/{rel}" if arc_prefix else rel
            zf.write(full_path, arcname)


def build_go_hub_archives() -> list[Path]:
    hub_dir = ROOT / "intelligent-hub"
    targets = [
        ("windows", "amd64", "intelligent-hub.exe"),
        ("linux", "amd64", "intelligent-hub"),
        ("darwin", "arm64", "intelligent-hub"),
    ]
    created: list[Path] = []
    tmp_bin_dir = OUT_DIR / "_tmp_go"
    tmp_bin_dir.mkdir(parents=True, exist_ok=True)
    try:
        for goos, goarch, bin_name in targets:
            bin_path = tmp_bin_dir / bin_name
            env = os.environ.copy()
            env["GOOS"] = goos
            env["GOARCH"] = goarch
            env["CGO_ENABLED"] = "0"
            subprocess.run(
                ["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(bin_path), "./cmd/intelligent-hub"],
                cwd=str(hub_dir),
                env=env,
                check=True,
            )
            zip_path = OUT_DIR / f"g2a-intelligent-hub-v0.0.1-{goos}-{goarch}.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(bin_path, bin_name)
                zf.write(hub_dir / "README.md", "README.md")
                zf.write(hub_dir / "LICENSE", "LICENSE")
            created.append(zip_path)
    finally:
        shutil.rmtree(tmp_bin_dir, ignore_errors=True)
    return created


def build_sdk_archives() -> list[Path]:
    origin_dir = ROOT / "interaction-origin"
    core_dir = ROOT / "computation-core"
    bundles = [
        ("g2a-origin-godot4-addon-v0.0.1.zip", origin_dir / "gdscript" / "addons" / "g2a_origin", "addons/g2a_origin"),
        ("g2a-origin-cpp17-headers-v0.0.1.zip", origin_dir / "cpp" / "include", "include"),
        ("g2a-origin-rust-crate-v0.0.1.zip", origin_dir / "rust", "g2a-origin-rust"),
        ("g2a-origin-csharp-unity-v0.0.1.zip", origin_dir / "csharp" / "G2A.InteractionOrigin", "G2A.InteractionOrigin"),
        ("g2a-origin-typescript-web-v0.0.1.zip", origin_dir / "typescript", "g2a-origin-typescript"),
        ("g2a-origin-python-v0.0.1.zip", origin_dir / "python", "g2a-origin-python"),
        ("g2a-computation-core-python-v0.0.1.zip", core_dir / "src", "src"),
        ("g2a-computation-core-typescript-v0.0.1.zip", core_dir / "typescript", "g2a-computation-core-typescript"),
        ("g2a-computation-core-go-v0.0.1.zip", core_dir / "go", "g2a-computation-core-go"),
        ("g2a-computation-core-csharp-v0.0.1.zip", core_dir / "csharp" / "G2A.ComputationCore", "G2A.ComputationCore"),
    ]
    created: list[Path] = []
    for zip_name, src_path, prefix in bundles:
        zip_path = OUT_DIR / zip_name
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            add_tree_to_zip(zf, src_path, prefix)
            if zip_name == "g2a-computation-core-python-v0.0.1.zip":
                zf.write(core_dir / "pyproject.toml", "pyproject.toml")
                zf.write(core_dir / "README.md", "README.md")
                zf.write(core_dir / "LICENSE", "LICENSE")
        created.append(zip_path)

    # Standalone Unity Package Manager (UPM) archive bundling Core C# SDK + Unity MonoBehaviours
    upm_zip = OUT_DIR / "g2a-origin-unity-upm-v0.0.1.zip"
    upm_src = origin_dir / "csharp" / "UnityPackage" / "com.g2a.interaction-origin"
    cs_core_src = origin_dir / "csharp" / "G2A.InteractionOrigin"
    with zipfile.ZipFile(upm_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        add_tree_to_zip(zf, upm_src, "com.g2a.interaction-origin")
        for cs_file in sorted(cs_core_src.glob("*.cs")):
            zf.write(cs_file, f"com.g2a.interaction-origin/Runtime/Core/{cs_file.name}")
    created.append(upm_zip)

    # Standalone Unreal Engine 5 Plugin archive bundling G2AInteractionOrigin + embedded C++17 headers
    ue_zip = OUT_DIR / "g2a-origin-unreal-plugin-v0.0.1.zip"
    ue_src = origin_dir / "cpp" / "UnrealPlugin" / "G2AInteractionOrigin"
    cpp_inc = origin_dir / "cpp" / "include" / "g2a"
    with zipfile.ZipFile(ue_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _, filenames in os.walk(ue_src):
            for fn in sorted(filenames):
                full = Path(dirpath) / fn
                rel = full.relative_to(ue_src).as_posix()
                if fn == "G2AInteractionOrigin.Build.cs":
                    txt = full.read_text(encoding="utf-8").replace(
                        'Path.Combine(ModuleDirectory, "..", "..", "..", "include")',
                        'Path.Combine(ModuleDirectory, "ThirdParty", "include")',
                    )
                    zf.writestr(f"G2AInteractionOrigin/{rel}", txt)
                else:
                    zf.write(full, f"G2AInteractionOrigin/{rel}")
        for hpp in sorted(cpp_inc.glob("*.hpp")):
            zf.write(hpp, f"G2AInteractionOrigin/Source/G2AInteractionOrigin/ThirdParty/include/g2a/{hpp.name}")
    created.append(ue_zip)

    return created


def write_sha256_sums(files: list[Path]) -> Path:
    sums_path = OUT_DIR / "SHA256SUMS.txt"
    lines: list[str] = []
    for f in sorted(files, key=lambda p: p.name):
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        lines.append(f"{digest}  {f.name}")
    sums_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return sums_path


def main() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    artifacts = build_go_hub_archives() + build_sdk_archives()
    sums = write_sha256_sums(artifacts)
    print(f"Packaged {len(artifacts)} G2A V0.0.1 release archives in {OUT_DIR}:")
    print(sums.read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    main()
