"""One-command polyglot build & verification runner for the G2A V0.0.1 three-application suite."""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def run_step(title: str, cmd: list[str], cwd: str) -> None:
    print(f"\n=== [{title}] ===", flush=True)
    res = subprocess.run(cmd, cwd=cwd)
    if res.returncode != 0:
        sys.exit(res.returncode)


def main() -> None:
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

    run_step("1/9 Go Intelligent Hub Unit Tests", ["go", "test", "-v", "./..."], hub_dir)
    run_step("2/9 Go Intelligent Hub Standalone Binary Build", ["go", "build", "-o", hub_exe, "./cmd/intelligent-hub"], hub_dir)
    run_step("3/9 C++17 Interaction Origin SDK + HTTP Transport Build & Self-Test", cpp_cmd, origin_dir)
    run_step("3b/9 Execute C++17 Self-Test", [cpp_exe], origin_dir)
    run_step("4/9 Rust Interaction Origin Crate (g2a-origin) Unit & Keep-Alive Tests", ["cargo", "test"], origin_rust_dir)
    run_step("5/9 C# (.NET 8 / Unity) Interaction Origin Build & Self-Test", ["dotnet", "run", "--project", "csharp/G2A.InteractionOrigin.SelfTest"], origin_dir)
    run_step("6/9 TypeScript (@g2a/interaction-origin) Node 20 Self-Test", ["node", "typescript/tests/self_test.mjs"], origin_dir)
    run_step("6b/9 Python Interaction Origin Unit Tests", [sys.executable, "-m", "unittest", "discover", "-s", "python/tests", "-v"], origin_dir)
    run_step("7/9 Go Computation Core SDK (g2acore) Unit & Live 3-Tier E2E Tests", ["go", "test", "-v", "./..."], core_go_dir)
    run_step("8/9 C# (.NET 8) Computation Core SDK Unit & Live 3-Tier E2E Tests", ["dotnet", "run", "--project", "csharp/G2A.ComputationCore.SelfTest"], core_dir)
    run_step("8b/9 TypeScript (@g2a/computation-core) Node 20 Live 3-Tier E2E Tests", ["node", "typescript/tests/self_test.mjs"], core_dir)
    run_step("9/9 Python Computation Core + 14-Error Conformance + NAT Relay + Polyglot E2E Suite", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], core_dir)

    print("\nALL G2A V0.0.1 POLYGLOT BUILDS & E2E VERIFICATIONS PASSED!", flush=True)


if __name__ == "__main__":
    main()
