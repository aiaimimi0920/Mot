"""
Full-ecosystem SDK & binary release packager for G2A V0.0.1:
  1. Intelligent Hub standalone binaries (6 OS/Arch targets compiled in parallel):
     - windows/amd64, windows/arm64, linux/amd64, linux/arm64, darwin/amd64, darwin/arm64
  2. Native Ecosystem Installable Packages:
     - PyPI PEP 427 Wheels (.whl) & PEP 625 Source Tarballs (.tar.gz) for `g2a-interaction-origin` & `g2a-computation-core`
     - NuGet Packages (.nupkg) & Symbol Packages (.snupkg) for `G2A.InteractionOrigin` & `G2A.ComputationCore`
     - npm Tarballs (.tgz) for `@g2a/interaction-origin` & `@g2a/computation-core`
     - Unity Package Manager Tarball (`com.g2a.interaction-origin-0.0.1.tgz`) + UPM `.zip`
     - Rust Cargo Crate (`g2a-origin-0.0.1.crate`) + Rust crate source `.zip`
     - C++17 CMake + pkg-config Package (`g2a-origin-cpp17-cmake-v0.0.1.zip`) + Header-only `.zip`
     - Unreal Engine 5 Standalone Plugin (`g2a-origin-unreal-plugin-v0.0.1.zip`)
     - Godot 4.x Addon (`g2a-origin-godot4-addon-v0.0.1.zip`)
     - Go Module Proxy Offline Bundle (`g2a-computation-core-go-proxy-v0.0.1.zip`) + Go module `.zip`
  3. Release Metadata & Offline Consumer Verification:
     - `release-manifest.json`, `INSTALL.zh-CN.md`, `INSTALL.md`, and `SHA256SUMS.txt`
     - Automatically runs `verify_packages.verify_release_dir()` to test offline `pip`, `node`, NuGet, UPM, Cargo, CMake, and Go Proxy artifacts!
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "release-artifacts" / "v0.0.1"

SKIP_DIRS = {"__pycache__", "bin", "obj", "build", "dist", "target", "node_modules", ".git", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".exe", ".dll", ".pdb", ".db", ".db-wal", ".db-shm", ".lock", ".tgz", ".nupkg", ".snupkg"}


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


def _urlsafe_sha256(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def build_go_hub_archives() -> list[tuple[Path, str, str]]:
    hub_dir = ROOT / "intelligent-hub"
    targets = [
        ("windows", "amd64", "intelligent-hub.exe"),
        ("windows", "arm64", "intelligent-hub.exe"),
        ("linux", "amd64", "intelligent-hub"),
        ("linux", "arm64", "intelligent-hub"),
        ("darwin", "amd64", "intelligent-hub"),
        ("darwin", "arm64", "intelligent-hub"),
    ]
    tmp_bin_dir = OUT_DIR / "_tmp_go"
    tmp_bin_dir.mkdir(parents=True, exist_ok=True)

    def _compile_one(item: tuple[str, str, str]) -> tuple[Path, str, str]:
        goos, goarch, bin_name = item
        slot_dir = tmp_bin_dir / f"{goos}-{goarch}"
        slot_dir.mkdir(parents=True, exist_ok=True)
        bin_path = slot_dir / bin_name
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
        return (zip_path, "intelligent-hub", f"binary-{goos}-{goarch}")

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            return list(pool.map(_compile_one, targets))
    finally:
        shutil.rmtree(tmp_bin_dir, ignore_errors=True)


def build_python_wheel_and_sdist(
    pkg_root: Path,
    dist_name: str,
    import_pkg: str,
    summary: str,
    license_id: str,
    console_script: tuple[str, str],
) -> list[tuple[Path, str, str]]:
    norm = dist_name.replace("-", "_")
    version = "0.0.1"
    dist_info = f"{norm}-{version}.dist-info"
    whl_path = OUT_DIR / f"{norm}-{version}-py3-none-any.whl"
    sdist_path = OUT_DIR / f"{norm}-{version}.tar.gz"
    tier = "interaction-origin" if "origin" in dist_name else "computation-core"

    readme_text = (pkg_root / "README.md").read_text(encoding="utf-8")
    metadata_bytes = (
        "Metadata-Version: 2.1\n"
        f"Name: {dist_name}\n"
        f"Version: {version}\n"
        f"Summary: {summary}\n"
        f"License: {license_id}\n"
        "Requires-Python: >=3.11\n"
        "Description-Content-Type: text/markdown\n\n"
        f"{readme_text.strip()}\n"
    ).encode("utf-8")
    wheel_bytes = (
        "Wheel-Version: 1.0\n"
        "Generator: g2a-build-releases (0.0.1)\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("utf-8")
    ep_bytes = f"[console_scripts]\n{console_script[0]} = {console_script[1]}\n".encode("utf-8")

    src_pkg_dir = pkg_root / "src" / import_pkg
    records: list[str] = []
    with zipfile.ZipFile(whl_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(src_pkg_dir):
            dirnames[:] = [d for d in sorted(dirnames) if d not in SKIP_DIRS]
            for fn in sorted(filenames):
                if fn.endswith((".pyc", ".pyo")):
                    continue
                full = Path(dirpath) / fn
                rel = full.relative_to(pkg_root / "src").as_posix()
                data = full.read_bytes()
                zf.writestr(rel, data)
                records.append(f"{rel},sha256={_urlsafe_sha256(data)},{len(data)}")

        for meta_rel, payload in (
            (f"{dist_info}/METADATA", metadata_bytes),
            (f"{dist_info}/WHEEL", wheel_bytes),
            (f"{dist_info}/entry_points.txt", ep_bytes),
        ):
            zf.writestr(meta_rel, payload)
            records.append(f"{meta_rel},sha256={_urlsafe_sha256(payload)},{len(payload)}")

        records.append(f"{dist_info}/RECORD,,")
        zf.writestr(f"{dist_info}/RECORD", ("\n".join(records) + "\n").encode("utf-8"))

    # PEP 625 Source Distribution (.tar.gz)
    sdist_prefix = f"{norm}-{version}"
    with tarfile.open(sdist_path, "w:gz") as tf:
        pkg_info_ti = tarfile.TarInfo(f"{sdist_prefix}/PKG-INFO")
        pkg_info_ti.size = len(metadata_bytes)
        tf.addfile(pkg_info_ti, io.BytesIO(metadata_bytes))
        for root_fn in ("pyproject.toml", "README.md"):
            p = pkg_root / root_fn
            if p.is_file():
                tf.add(p, arcname=f"{sdist_prefix}/{root_fn}")
        for dirpath, dirnames, filenames in os.walk(src_pkg_dir):
            dirnames[:] = [d for d in sorted(dirnames) if d not in SKIP_DIRS]
            for fn in sorted(filenames):
                if fn.endswith((".pyc", ".pyo")):
                    continue
                full = Path(dirpath) / fn
                rel = full.relative_to(pkg_root).as_posix()
                tf.add(full, arcname=f"{sdist_prefix}/{rel}")

    return [
        (whl_path, tier, "python-wheel"),
        (sdist_path, tier, "python-sdist"),
    ]


def build_nuget_packages() -> list[tuple[Path, str, str]]:
    tmp_nu = OUT_DIR / "_tmp_nuget"
    tmp_nu.mkdir(parents=True, exist_ok=True)
    projects = [
        (
            ROOT / "interaction-origin" / "csharp" / "G2A.InteractionOrigin" / "G2A.InteractionOrigin.csproj",
            "interaction-origin",
        ),
        (
            ROOT / "computation-core" / "csharp" / "G2A.ComputationCore" / "G2A.ComputationCore.csproj",
            "computation-core",
        ),
    ]
    out: list[tuple[Path, str, str]] = []
    try:
        for csproj, tier in projects:
            subprocess.run(
                ["dotnet", "pack", str(csproj), "-c", "Release", "--nologo", "-o", str(tmp_nu)],
                cwd=str(csproj.parent),
                check=True,
            )
        for f in sorted(tmp_nu.iterdir()):
            if f.suffix in (".nupkg", ".snupkg"):
                dst = OUT_DIR / f.name
                shutil.move(str(f), str(dst))
                tier = "interaction-origin" if "InteractionOrigin" in f.name else "computation-core"
                kind = "nuget-symbols" if f.suffix == ".snupkg" else "nuget-package"
                out.append((dst, tier, kind))
    finally:
        shutil.rmtree(tmp_nu, ignore_errors=True)
    return out


def build_npm_tarballs() -> list[tuple[Path, str, str]]:
    npm_cmd = shutil.which("npm") or "npm.cmd"
    targets = [
        (ROOT / "interaction-origin" / "typescript", "g2a-interaction-origin-0.0.1.tgz", "interaction-origin"),
        (ROOT / "computation-core" / "typescript", "g2a-computation-core-0.0.1.tgz", "computation-core"),
    ]
    out: list[tuple[Path, str, str]] = []
    for pkg_dir, expected_name, tier in targets:
        subprocess.run(
            [npm_cmd, "pack", "--quiet", "--pack-destination", str(OUT_DIR)],
            cwd=str(pkg_dir),
            check=True,
        )
        out.append((OUT_DIR / expected_name, tier, "npm-tarball"))
    return out


def build_unity_upm_tarball_and_zip() -> list[tuple[Path, str, str]]:
    origin_dir = ROOT / "interaction-origin"
    upm_src = origin_dir / "csharp" / "UnityPackage" / "com.g2a.interaction-origin"
    cs_core_src = origin_dir / "csharp" / "G2A.InteractionOrigin"

    upm_zip = OUT_DIR / "g2a-origin-unity-upm-v0.0.1.zip"
    upm_tgz = OUT_DIR / "com.g2a.interaction-origin-0.0.1.tgz"

    with zipfile.ZipFile(upm_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        add_tree_to_zip(zf, upm_src, "com.g2a.interaction-origin")
        for cs_file in sorted(cs_core_src.glob("*.cs")):
            zf.write(cs_file, f"com.g2a.interaction-origin/Runtime/Core/{cs_file.name}")

    # Unity Package Manager tarball format requires root directory `package/`
    raw_tar = io.BytesIO()
    with tarfile.open(fileobj=raw_tar, mode="w") as tf:
        for dirpath, dirnames, filenames in os.walk(upm_src):
            dirnames[:] = [d for d in sorted(dirnames) if d not in SKIP_DIRS]
            for fn in sorted(filenames):
                full = Path(dirpath) / fn
                rel = full.relative_to(upm_src).as_posix()
                tf.add(full, arcname=f"package/{rel}")
        for cs_file in sorted(cs_core_src.glob("*.cs")):
            tf.add(cs_file, arcname=f"package/Runtime/Core/{cs_file.name}")
    with gzip.GzipFile(filename="com.g2a.interaction-origin-0.0.1.tar", mode="wb", fileobj=upm_tgz.open("wb"), mtime=0) as gz:
        gz.write(raw_tar.getvalue())

    return [
        (upm_tgz, "interaction-origin", "unity-upm-tarball"),
        (upm_zip, "interaction-origin", "unity-upm-zip"),
    ]


def build_rust_crate() -> tuple[Path, str, str]:
    rust_dir = ROOT / "interaction-origin" / "rust"
    cargo_cmd = shutil.which("cargo") or "cargo"
    subprocess.run(
        [cargo_cmd, "package", "--quiet", "--no-verify", "--allow-dirty"],
        cwd=str(rust_dir),
        check=True,
    )
    built_crate = rust_dir / "target" / "package" / "g2a-origin-0.0.1.crate"
    dst = OUT_DIR / "g2a-origin-0.0.1.crate"
    shutil.copy2(built_crate, dst)
    shutil.rmtree(rust_dir / "target" / "package", ignore_errors=True)
    return (dst, "interaction-origin", "rust-cargo-crate")


def build_go_module_proxy_archive() -> tuple[Path, str, str]:
    go_dir = ROOT / "computation-core" / "go"
    mod_path = "github.com/aiaimimi0920/g2a-computation-core"
    ver = "v0.0.1"

    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, "w", compression=zipfile.ZIP_DEFLATED) as izf:
        for fn in sorted(os.listdir(go_dir)):
            full = go_dir / fn
            if full.is_file() and not any(fn.endswith(s) for s in SKIP_SUFFIXES):
                izf.write(full, f"{mod_path}@{ver}/{fn}")

    proxy_zip = OUT_DIR / "g2a-computation-core-go-proxy-v0.0.1.zip"
    base = f"{mod_path}/@v"
    info_json = json.dumps({"Version": ver, "Time": "2026-11-26T00:00:00Z"}, separators=(",", ":")).encode("utf-8")
    with zipfile.ZipFile(proxy_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{base}/list", f"{ver}\n".encode("utf-8"))
        zf.writestr(f"{base}/{ver}.info", info_json)
        zf.write(go_dir / "go.mod", f"{base}/{ver}.mod")
        zf.writestr(f"{base}/{ver}.zip", inner_buf.getvalue())
    return (proxy_zip, "computation-core", "go-module-proxy")


def build_engine_and_source_archives() -> list[tuple[Path, str, str]]:
    origin_dir = ROOT / "interaction-origin"
    core_dir = ROOT / "computation-core"
    bundles = [
        ("g2a-origin-godot4-addon-v0.0.1.zip", origin_dir / "gdscript" / "addons" / "g2a_origin", "addons/g2a_origin", "interaction-origin", "godot4-addon"),
        ("g2a-origin-cpp17-headers-v0.0.1.zip", origin_dir / "cpp" / "include", "include", "interaction-origin", "cpp17-headers"),
        ("g2a-origin-rust-crate-v0.0.1.zip", origin_dir / "rust", "g2a-origin-rust", "interaction-origin", "rust-source"),
        ("g2a-origin-csharp-unity-v0.0.1.zip", origin_dir / "csharp" / "G2A.InteractionOrigin", "G2A.InteractionOrigin", "interaction-origin", "csharp-source"),
        ("g2a-origin-typescript-web-v0.0.1.zip", origin_dir / "typescript", "g2a-origin-typescript", "interaction-origin", "typescript-source"),
        ("g2a-origin-python-v0.0.1.zip", origin_dir / "python", "g2a-origin-python", "interaction-origin", "python-source"),
        ("g2a-computation-core-python-v0.0.1.zip", core_dir / "src", "src", "computation-core", "python-source"),
        ("g2a-computation-core-typescript-v0.0.1.zip", core_dir / "typescript", "g2a-computation-core-typescript", "computation-core", "typescript-source"),
        ("g2a-computation-core-go-v0.0.1.zip", core_dir / "go", "g2a-computation-core-go", "computation-core", "go-source"),
        ("g2a-computation-core-csharp-v0.0.1.zip", core_dir / "csharp" / "G2A.ComputationCore", "G2A.ComputationCore", "computation-core", "csharp-source"),
    ]
    created: list[tuple[Path, str, str]] = []
    for zip_name, src_path, prefix, tier, kind in bundles:
        zip_path = OUT_DIR / zip_name
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            add_tree_to_zip(zf, src_path, prefix)
            if zip_name == "g2a-computation-core-python-v0.0.1.zip":
                zf.write(core_dir / "pyproject.toml", "pyproject.toml")
                zf.write(core_dir / "README.md", "README.md")
                zf.write(core_dir / "LICENSE", "LICENSE")
        created.append((zip_path, tier, kind))

    # C++17 CMake + pkg-config installable package archive
    cmake_zip = OUT_DIR / "g2a-origin-cpp17-cmake-v0.0.1.zip"
    cpp_dir = origin_dir / "cpp"
    with zipfile.ZipFile(cmake_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(cpp_dir / "CMakeLists.txt", "g2a-origin-cpp17/CMakeLists.txt")
        zf.write(cpp_dir / "README.md", "g2a-origin-cpp17/README.md")
        add_tree_to_zip(zf, cpp_dir / "cmake", "g2a-origin-cpp17/cmake")
        add_tree_to_zip(zf, cpp_dir / "include", "g2a-origin-cpp17/include")
        add_tree_to_zip(zf, cpp_dir / "tests", "g2a-origin-cpp17/tests")
    created.append((cmake_zip, "interaction-origin", "cpp17-cmake-package"))

    # Unreal Engine 5 standalone plugin archive with embedded C++17 headers
    ue_zip = OUT_DIR / "g2a-origin-unreal-plugin-v0.0.1.zip"
    ue_src = cpp_dir / "UnrealPlugin" / "G2AInteractionOrigin"
    cpp_inc = cpp_dir / "include" / "g2a"
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
    created.append((ue_zip, "interaction-origin", "unreal-engine-5-plugin"))

    return created


def write_install_guides_and_manifest(items: list[tuple[Path, str, str]]) -> Path:
    sorted_items = sorted(items, key=lambda t: t[0].name)
    sums_lines: list[str] = []
    manifest_entries: list[dict[str, object]] = []

    for fpath, tier, kind in sorted_items:
        raw = fpath.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        sums_lines.append(f"{digest}  {fpath.name}")
        manifest_entries.append(
            {
                "filename": fpath.name,
                "tier": tier,
                "format": kind,
                "size_bytes": len(raw),
                "sha256": digest,
            }
        )

    sums_path = OUT_DIR / "SHA256SUMS.txt"
    sums_path.write_text("\n".join(sums_lines) + "\n", encoding="utf-8", newline="\n")

    manifest_path = OUT_DIR / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "g2a_version": "0.0.1",
                "artifact_count": len(manifest_entries),
                "golden_jcs_confirm_v1": "sha256:8d8634fd72ae48e655a9151e50b06a3e1095e753cacb2c293706e59af1b9ccd8",
                "artifacts": manifest_entries,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    install_zh = """# G2A V0.0.1 全生态 SDK 与智能枢纽安装指南

## 1. 智能枢纽（Intelligent Hub — 6 平台独立二进制）
解压对应操作系统的 `g2a-intelligent-hub-v0.0.1-<os>-<arch>.zip` 并直接运行 `intelligent-hub`（支持 `windows-amd64`、`windows-arm64`、`linux-amd64`、`linux-arm64`、`darwin-amd64`、`darwin-arm64`）。

## 2. 交互源点 SDK（Interaction Origin — 6 语言/引擎原生包）
- **Python (Wheel / sdist):** `pip install g2a_interaction_origin-0.0.1-py3-none-any.whl`
- **Unity (UPM Tarball):** Unity 编辑器打开 `Window -> Package Manager -> + -> Add package from tarball...`，选择 `com.g2a.interaction-origin-0.0.1.tgz`
- **C# / .NET 8 (NuGet):** `dotnet add package G2A.InteractionOrigin --version 0.0.1 --source .`
- **Unreal Engine 5 (Plugin):** 解压 `g2a-origin-unreal-plugin-v0.0.1.zip` 至工程 `Plugins/G2AInteractionOrigin/`
- **C++17 (CMake / pkg-config):** 解压 `g2a-origin-cpp17-cmake-v0.0.1.zip`，执行 `cmake -B build && cmake --install build`，使用 `find_package(G2AOrigin 0.0.1 REQUIRED)`
- **Godot 4.x (Addon):** 解压 `g2a-origin-godot4-addon-v0.0.1.zip` 至工程根目录 `addons/g2a_origin/` 并启用插件
- **Rust (Cargo `.crate`):** 解压 `g2a-origin-0.0.1.crate` 或在 `Cargo.toml` 中通过 `path` 引用
- **TypeScript / Web / Tauri (npm):** `npm install ./g2a-interaction-origin-0.0.1.tgz`

## 3. 演算中枢 SDK（Computation Core — 4 语言原生包）
- **Python (Wheel / sdist):** `pip install g2a_computation_core-0.0.1-py3-none-any.whl`
- **TypeScript / Node 20+ / Deno / Bun (npm):** `npm install ./g2a-computation-core-0.0.1.tgz`
- **Go 1.22+ (GOPROXY 离线源):** 解压 `g2a-computation-core-go-proxy-v0.0.1.zip` 至 `<dir>`，执行 `GOPROXY=file:///<dir> go get github.com/aiaimimi0920/g2a-computation-core@v0.0.1`
- **C# / .NET 8 (NuGet):** `dotnet add package G2A.ComputationCore --version 0.0.1 --source .`
"""
    install_en = """# G2A V0.0.1 Polyglot SDK & Intelligent Hub Installation Guide

## 1. Intelligent Hub (6 Standalone OS/Arch Binaries)
Extract `g2a-intelligent-hub-v0.0.1-<os>-<arch>.zip` and run `intelligent-hub` (`windows-amd64`, `windows-arm64`, `linux-amd64`, `linux-arm64`, `darwin-amd64`, `darwin-arm64`).

## 2. Interaction Origin SDKs (6 Engines / Languages)
- **Python (PEP 427 Wheel):** `pip install g2a_interaction_origin-0.0.1-py3-none-any.whl`
- **Unity (UPM Tarball):** `Window -> Package Manager -> + -> Add package from tarball...` -> `com.g2a.interaction-origin-0.0.1.tgz`
- **C# / .NET 8 (NuGet):** `dotnet add package G2A.InteractionOrigin --version 0.0.1 --source .`
- **Unreal Engine 5 (Plugin):** Extract `g2a-origin-unreal-plugin-v0.0.1.zip` into `Plugins/G2AInteractionOrigin/`
- **C++17 (CMake / pkg-config):** Extract `g2a-origin-cpp17-cmake-v0.0.1.zip`, run `cmake -B build && cmake --install build`, then `find_package(G2AOrigin 0.0.1 REQUIRED)`
- **Godot 4.x (Addon):** Extract `g2a-origin-godot4-addon-v0.0.1.zip` into `addons/g2a_origin/`
- **Rust (Cargo `.crate`):** Extract `g2a-origin-0.0.1.crate` or reference via `Cargo.toml`
- **TypeScript / Web / Tauri (npm):** `npm install ./g2a-interaction-origin-0.0.1.tgz`

## 3. Computation Core SDKs (4 Languages)
- **Python (PEP 427 Wheel):** `pip install g2a_computation_core-0.0.1-py3-none-any.whl`
- **TypeScript / Node 20+ / Deno / Bun (npm):** `npm install ./g2a-computation-core-0.0.1.tgz`
- **Go 1.22+ (Offline GOPROXY):** Extract `g2a-computation-core-go-proxy-v0.0.1.zip` to `<dir>`, then `GOPROXY=file:///<dir> go get github.com/aiaimimi0920/g2a-computation-core@v0.0.1`
- **C# / .NET 8 (NuGet):** `dotnet add package G2A.ComputationCore --version 0.0.1 --source .`
"""
    (OUT_DIR / "INSTALL.zh-CN.md").write_text(install_zh, encoding="utf-8", newline="\n")
    (OUT_DIR / "INSTALL.md").write_text(install_en, encoding="utf-8", newline="\n")
    return sums_path


def main() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_artifacts: list[tuple[Path, str, str]] = []
    all_artifacts.extend(build_go_hub_archives())
    all_artifacts.extend(
        build_python_wheel_and_sdist(
            ROOT / "interaction-origin" / "python",
            "g2a-interaction-origin",
            "g2a_origin",
            "Zero-dependency G2A V0.0.1 Interaction Origin SDK and 5-genre host simulator",
            "Apache-2.0",
            ("g2a-origin-demo", "g2a_origin.demo_host:main"),
        )
    )
    all_artifacts.extend(
        build_python_wheel_and_sdist(
            ROOT / "computation-core",
            "g2a-computation-core",
            "g2a_core",
            "Zero-dependency G2A V0.0.1 Computation Core SDK, LLM firewall, NAT relay, and SQLite portable memory",
            "LGPL-3.0-only",
            ("g2a-core", "g2a_core.cli:main"),
        )
    )
    all_artifacts.extend(build_nuget_packages())
    all_artifacts.extend(build_npm_tarballs())
    all_artifacts.extend(build_unity_upm_tarball_and_zip())
    all_artifacts.append(build_rust_crate())
    all_artifacts.append(build_go_module_proxy_archive())
    all_artifacts.extend(build_engine_and_source_archives())

    sums = write_install_guides_and_manifest(all_artifacts)
    print(f"Packaged {len(all_artifacts)} G2A V0.0.1 native SDK packages & binaries in {OUT_DIR}:")
    print(sums.read_text(encoding="utf-8").strip())

    # Run offline consumer-install & structural integrity checks automatically
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import verify_packages

    verify_packages.verify_release_dir(OUT_DIR)


if __name__ == "__main__":
    main()
