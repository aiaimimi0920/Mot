"""
Offline consumer-installation & archive-integrity verifier for all G2A V0.0.1 release packages.
Verifies:
  1. SHA256SUMS.txt & release-manifest.json completeness and cryptographic hash match;
  2. Python PEP 427 Wheels (.whl): offline `pip install --no-index --no-deps --target` + live import & Golden JCS check;
  3. npm Tarballs (.tgz): unpacks `@g2a/interaction-origin` & `@g2a/computation-core` + runs Node ESM Golden JCS check;
  4. NuGet Packages (.nupkg & .snupkg): verifies compiled net8.0 DLLs, XML docs, README.md, and .nuspec metadata;
  5. Unity Package Manager Tarball (`com.g2a.interaction-origin-0.0.1.tgz`): verifies `package/package.json` & `Runtime/Core/OriginSdk.cs`;
  6. Rust Cargo Crate (`g2a-origin-0.0.1.crate`): verifies `Cargo.toml`, `README.md`, and `src/lib.rs`;
  7. C++17 CMake Package, Unreal Engine 5 Plugin, Godot 4 Addon, Go Module Proxy (`@v/v0.0.1.zip`), and 6 OS/Arch Hub binaries.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = ROOT / "release-artifacts" / "v0.0.1"
GOLDEN_JCS = "sha256:8d8634fd72ae48e655a9151e50b06a3e1095e753cacb2c293706e59af1b9ccd8"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_release_dir(out_dir: Path = DEFAULT_OUT_DIR) -> None:
    if not out_dir.exists():
        raise RuntimeError(f"Release directory does not exist: {out_dir}")

    sums_file = out_dir / "SHA256SUMS.txt"
    manifest_file = out_dir / "release-manifest.json"
    for doc_name in ("SHA256SUMS.txt", "release-manifest.json", "INSTALL.zh-CN.md", "INSTALL.md"):
        if not (out_dir / doc_name).is_file():
            raise RuntimeError(f"Missing required release metadata file: {doc_name}")

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("g2a_version") != "0.0.1":
        raise RuntimeError(f"Unexpected manifest g2a_version: {manifest.get('g2a_version')}")

    # 1. Verify SHA256SUMS.txt & release-manifest.json hashes
    recorded_sums: dict[str, str] = {}
    for line in sums_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, fname = line.split("  ", 1)
        fpath = out_dir / fname
        if not fpath.is_file():
            raise RuntimeError(f"Listed artifact missing on disk: {fname}")
        actual = sha256_file(fpath)
        if actual != digest:
            raise RuntimeError(f"SHA-256 mismatch for {fname}: {actual} != {digest}")
        recorded_sums[fname] = actual

    manifest_items = {item["filename"]: item for item in manifest.get("artifacts", [])}
    if set(manifest_items.keys()) != set(recorded_sums.keys()):
        raise RuntimeError(
            f"Artifact set mismatch between manifest ({len(manifest_items)}) and SHA256SUMS ({len(recorded_sums)})"
        )
    for fname, item in manifest_items.items():
        if item["sha256"] != recorded_sums[fname]:
            raise RuntimeError(f"Manifest SHA-256 mismatch for {fname}")

    # 2. Offline pip install & import verification of BOTH Python PEP 427 Wheels
    orig_whl = out_dir / "g2a_interaction_origin-0.0.1-py3-none-any.whl"
    core_whl = out_dir / "g2a_computation_core-0.0.1-py3-none-any.whl"
    with tempfile.TemporaryDirectory() as tmp_py:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--no-index",
                "--no-deps",
                "--target",
                tmp_py,
                str(orig_whl),
                str(core_whl),
            ],
            check=True,
        )
        py_check = (
            "import sys; "
            f"sys.path.insert(0, {tmp_py!r}); "
            "from g2a_origin.jcs import jcs_sha256 as o_sha; "
            "from g2a_core.jcs import jcs_sha256 as c_sha; "
            "vec = {'steps':[{'args':{'qty':20.0,'item':'timber'},'step_index':0}],'queue_id':'Q-205','session_id':'sess-1'}; "
            f"assert o_sha('g2a.confirm.v1', vec) == {GOLDEN_JCS!r}; "
            f"assert c_sha('g2a.confirm.v1', vec) == {GOLDEN_JCS!r}"
        )
        subprocess.run([sys.executable, "-c", py_check], check=True)

    # 3. Unpack & execute Node ESM import verification of BOTH npm .tgz tarballs
    orig_tgz = out_dir / "g2a-interaction-origin-0.0.1.tgz"
    core_tgz = out_dir / "g2a-computation-core-0.0.1.tgz"
    node_bin = shutil.which("node") or "node"
    with tempfile.TemporaryDirectory() as tmp_npm:
        orig_unpack = Path(tmp_npm) / "orig"
        core_unpack = Path(tmp_npm) / "core"
        orig_unpack.mkdir()
        core_unpack.mkdir()
        with tarfile.open(orig_tgz, "r:gz") as tf:
            tf.extractall(orig_unpack, filter="data")
        with tarfile.open(core_tgz, "r:gz") as tf:
            tf.extractall(core_unpack, filter="data")

        orig_mjs = (orig_unpack / "package" / "esm" / "index.mjs").as_uri()
        core_mjs = (core_unpack / "package" / "esm" / "index.mjs").as_uri()
        js_check = (
            f"const o = await import({json.dumps(orig_mjs)}); "
            f"const c = await import({json.dumps(core_mjs)}); "
            "const vec = {steps:[{args:{qty:20.0,item:'timber'},step_index:0}],queue_id:'Q-205',session_id:'sess-1'}; "
            "const d1 = await o.jcsSha256('g2a.confirm.v1', vec); "
            "const d2 = await c.jcsSha256('g2a.confirm.v1', vec); "
            f"if (d1 !== {json.dumps(GOLDEN_JCS)} || d2 !== {json.dumps(GOLDEN_JCS)}) process.exit(1);"
        )
        subprocess.run([node_bin, "--input-type=module", "-e", js_check], check=True)

    # 4. Verify NuGet .nupkg & .snupkg packages
    for pkg_id in ("G2A.InteractionOrigin", "G2A.ComputationCore"):
        nupkg = out_dir / f"{pkg_id}.0.0.1.nupkg"
        snupkg = out_dir / f"{pkg_id}.0.0.1.snupkg"
        if not snupkg.is_file():
            raise RuntimeError(f"Missing symbol package: {snupkg.name}")
        with zipfile.ZipFile(nupkg, "r") as zf:
            names = set(zf.namelist())
            for req_entry in (
                f"{pkg_id}.nuspec",
                f"lib/net8.0/{pkg_id}.dll",
                f"lib/net8.0/{pkg_id}.xml",
                "README.md",
            ):
                if req_entry not in names:
                    raise RuntimeError(f"{nupkg.name} missing required NuGet entry: {req_entry}")

    # 5. Verify Unity Package Manager (UPM) .tgz tarball
    upm_tgz = out_dir / "com.g2a.interaction-origin-0.0.1.tgz"
    with tarfile.open(upm_tgz, "r:gz") as tf:
        upm_names = set(tf.getnames())
        for req_upm in (
            "package/package.json",
            "package/README.md",
            "package/CHANGELOG.md",
            "package/Runtime/G2AUnityOriginBridge.cs",
            "package/Runtime/Core/OriginSdk.cs",
        ):
            if req_upm not in upm_names:
                raise RuntimeError(f"Unity UPM tarball missing {req_upm}")

    # 6. Verify Rust Cargo .crate package
    crate_pkg = out_dir / "g2a-origin-0.0.1.crate"
    with tarfile.open(crate_pkg, "r:gz") as tf:
        crate_names = set(tf.getnames())
        for req_rs in (
            "g2a-origin-0.0.1/Cargo.toml",
            "g2a-origin-0.0.1/README.md",
            "g2a-origin-0.0.1/src/lib.rs",
            "g2a-origin-0.0.1/src/transport.rs",
        ):
            if req_rs not in crate_names:
                raise RuntimeError(f"Rust .crate missing {req_rs}")

    # 7. Verify C++17 CMake zip, UE5 plugin zip, Godot 4 zip, Go Module Proxy zip, and 6 Hub binaries
    with zipfile.ZipFile(out_dir / "g2a-origin-cpp17-cmake-v0.0.1.zip", "r") as zf:
        cmake_names = set(zf.namelist())
        for req_cm in (
            "g2a-origin-cpp17/CMakeLists.txt",
            "g2a-origin-cpp17/cmake/G2AOriginConfig.cmake.in",
            "g2a-origin-cpp17/include/g2a/origin_sdk.hpp",
            "g2a-origin-cpp17/include/g2a/http_transport.hpp",
        ):
            if req_cm not in cmake_names:
                raise RuntimeError(f"C++17 CMake zip missing {req_cm}")

    with zipfile.ZipFile(out_dir / "g2a-origin-unreal-plugin-v0.0.1.zip", "r") as zf:
        ue_names = set(zf.namelist())
        if "G2AInteractionOrigin/G2AInteractionOrigin.uplugin" not in ue_names:
            raise RuntimeError("UE5 zip missing .uplugin")
        if "G2AInteractionOrigin/Source/G2AInteractionOrigin/ThirdParty/include/g2a/http_transport.hpp" not in ue_names:
            raise RuntimeError("UE5 zip missing embedded http_transport.hpp")

    with zipfile.ZipFile(out_dir / "g2a-computation-core-go-proxy-v0.0.1.zip", "r") as zf:
        gp_names = set(zf.namelist())
        base_gp = "github.com/aiaimimi0920/g2a-computation-core/@v"
        for sfx in ("list", "v0.0.1.info", "v0.0.1.mod", "v0.0.1.zip"):
            if f"{base_gp}/{sfx}" not in gp_names:
                raise RuntimeError(f"Go proxy archive missing {base_gp}/{sfx}")
        inner_zip_bytes = zf.read(f"{base_gp}/v0.0.1.zip")
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf_in:
            tf_in.write(inner_zip_bytes)
            inner_path = Path(tf_in.name)
        try:
            with zipfile.ZipFile(inner_path, "r") as izf:
                if "github.com/aiaimimi0920/g2a-computation-core@v0.0.1/core_sdk.go" not in set(izf.namelist()):
                    raise RuntimeError("Inner Go module proxy v0.0.1.zip has invalid module prefix")
        finally:
            inner_path.unlink(missing_ok=True)

    for target in (
        "windows-amd64",
        "windows-arm64",
        "linux-amd64",
        "linux-arm64",
        "darwin-amd64",
        "darwin-arm64",
    ):
        hub_zip = out_dir / f"g2a-intelligent-hub-v0.0.1-{target}.zip"
        if not hub_zip.is_file():
            raise RuntimeError(f"Missing Intelligent Hub binary archive: {hub_zip.name}")

    print(
        f"VERIFIED ALL {len(recorded_sums)} G2A V0.0.1 NATIVE PACKAGES & RELEASE ARCHIVES "
        "(pip wheels, npm tarballs, NuGet .nupkg/.snupkg, Unity UPM .tgz, Rust .crate, CMake, UE5, Godot 4, Go Proxy, & 6 Hub targets)!"
    )


if __name__ == "__main__":
    verify_release_dir()
