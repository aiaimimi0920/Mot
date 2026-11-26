"""One-command parallelized polyglot build & verification runner for the G2A V0.0.1 three-application suite."""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def run_step(title: str, cmd: list[str], cwd: str) -> None:
    t0 = time.perf_counter()
    print(f"\n=== [{title}] ===", flush=True)
    res = subprocess.run(cmd, cwd=cwd)
    if res.returncode != 0:
        sys.exit(res.returncode)
    print(f"--- [PASS {time.perf_counter() - t0:.2f}s] {title} ---", flush=True)


def run_buffered_track(track_name: str, steps: list[tuple[str, list[str], str]]) -> tuple[str, float, int, str]:
    t0 = time.perf_counter()
    chunks: list[str] = []
    for title, cmd, cwd in steps:
        st0 = time.perf_counter()
        res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        dt = time.perf_counter() - st0
        status = "PASS" if res.returncode == 0 else f"FAIL({res.returncode})"
        chunks.append(f"\n=== [{title}] ({status} {dt:.2f}s) ===\n{res.stdout.rstrip()}")
        if res.returncode != 0:
            return track_name, time.perf_counter() - t0, res.returncode, "\n".join(chunks)
    return track_name, time.perf_counter() - t0, 0, "\n".join(chunks)


def main() -> None:
    suite_t0 = time.perf_counter()
    hub_dir = os.path.join(ROOT, "intelligent-hub")
    origin_dir = os.path.join(ROOT, "interaction-origin")
    origin_rust_dir = os.path.join(origin_dir, "rust")
    core_dir = os.path.join(ROOT, "computation-core")
    core_go_dir = os.path.join(core_dir, "go")
    cpp_build_dir = os.path.join(origin_dir, "cpp", "build")
    os.makedirs(cpp_build_dir, exist_ok=True)
    cpp_exe = os.path.join(cpp_build_dir, "self_test.exe" if os.name == "nt" else "self_test")
    hub_exe = os.path.join("bin", "intelligent-hub.exe" if os.name == "nt" else "intelligent-hub")

    cpp_cmd = ["g++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "cpp/tests/self_test.cpp", "-o", cpp_exe]
    if os.name == "nt":
        cpp_cmd.append("-lws2_32")

    # Phase 1: Build & verify Intelligent Hub binary first (required by live 3-tier E2E suites)
    run_step("1/9 Go Intelligent Hub Unit Tests", ["go", "test", "-v", "./..."], hub_dir)
    run_step("2/9 Go Intelligent Hub Standalone Binary Build", ["go", "build", "-o", hub_exe, "./cmd/intelligent-hub"], hub_dir)

    # Phase 2: Run independent polyglot SDK builds & E2E tracks in parallel (grouping C# tracks to avoid shared csproj file locks)
    parallel_tracks: list[tuple[str, list[tuple[str, list[str], str]]]] = [
        (
            "C++17 Origin Track",
            [
                ("3/9 C++17 Interaction Origin SDK + HTTP Transport Build", cpp_cmd, origin_dir),
                ("3b/9 Execute C++17 Self-Test", [cpp_exe], origin_dir),
            ],
        ),
        (
            "Rust Origin Track",
            [
                ("4/9 Rust Interaction Origin Crate (g2a-origin) Unit & Keep-Alive Tests", ["cargo", "test"], origin_rust_dir),
            ],
        ),
        (
            "C# (.NET 8) Origin & Core Track",
            [
                ("5/9 C# (.NET 8 / Unity) Interaction Origin Build & Self-Test", ["dotnet", "run", "--project", "csharp/G2A.InteractionOrigin.SelfTest"], origin_dir),
                ("8/9 C# (.NET 8) Computation Core SDK Unit & Live 3-Tier E2E Tests", ["dotnet", "run", "--project", "csharp/G2A.ComputationCore.SelfTest"], core_dir),
            ],
        ),
        (
            "TypeScript & Python Origin + Go Core Track",
            [
                ("6/9 TypeScript (@g2a/interaction-origin) Node Self-Test", ["node", "typescript/tests/self_test.mjs"], origin_dir),
                ("6b/9 Python Interaction Origin Unit Tests", [sys.executable, "-m", "unittest", "discover", "-s", "python/tests", "-v"], origin_dir),
                ("7/9 Go Computation Core SDK (g2acore) Unit & Live 3-Tier E2E Tests", ["go", "test", "-v", "./..."], core_go_dir),
                ("8b/9 TypeScript (@g2a/computation-core) Node Live 3-Tier E2E Tests", ["node", "typescript/tests/self_test.mjs"], core_dir),
            ],
        ),
    ]

    print("\n=== [Parallel Polyglot SDK Build & E2E Matrix (4 concurrent tracks)] ===", flush=True)
    failed_rc = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run_buffered_track, name, steps) for name, steps in parallel_tracks]
        for fut in futures:
            name, elapsed, rc, log_text = fut.result()
            print(log_text, flush=True)
            print(f"--- [{'PASS' if rc == 0 else 'FAIL'} {elapsed:.2f}s] {name} ---", flush=True)
            if rc != 0 and failed_rc == 0:
                failed_rc = rc
    if failed_rc != 0:
        sys.exit(failed_rc)

    # Phase 3: Full Python Computation Core + 14-Error Conformance + NAT Relay + Polyglot E2E Suite
    run_step(
        "9/9 Python Computation Core + 14-Error Conformance + NAT Relay + Polyglot E2E Suite",
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        core_dir,
    )

    total_dt = time.perf_counter() - suite_t0
    print(f"\nALL G2A V0.0.1 POLYGLOT BUILDS & E2E VERIFICATIONS PASSED in {total_dt:.2f}s!", flush=True)


if __name__ == "__main__":
    main()
