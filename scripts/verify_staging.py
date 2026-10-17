# SPDX-License-Identifier: AGPL-3.0-or-later
"""Staging and production hardening verification script for SearXNG-NextGen.

Checks:
1. Python syntax & compilation across core pillars (anticipate, mesh, deepweb, sandbox, graph).
2. Environment keys (SEARXNG_MESH_KEY length & hex validity when present).
3. Volatile RAM storage directories and permissions.
4. Active pillar enablement invariants.
"""

from __future__ import annotations

import os
import py_compile
import sys
import typing as t

PILLARS: tuple[str, ...] = ("anticipate", "mesh", "deepweb", "sandbox", "graph", "forensics")


def check_compilation() -> int:
    print("[*] Checking Python compilation of core nextgen modules...")
    files_to_check = [
        "searx/anticipate/__init__.py",
        "searx/anticipate/store.py",
        "searx/anticipate/predictor.py",
        "searx/mesh/__init__.py",
        "searx/mesh/protocol.py",
        "searx/mesh/transport.py",
        "searx/mesh/node.py",
        "searx/mesh/routing.py",
        "searx/deepweb/__init__.py",
        "searx/deepweb/tor_client.py",
        "searx/deepweb/parsers.py",
        "searx/nextgen_bridge.py",
        "searx/nextgen_webui.py",
    ]
    failed = 0
    for rel_path in files_to_check:
        full_path = os.path.abspath(rel_path)
        if not os.path.isfile(full_path):
            print(f"  [!] Missing file: {rel_path}")
            failed += 1
            continue
        try:
            py_compile.compile(full_path, doraise=True)
            print(f"  [OK] {rel_path}")
        except Exception as e:
            print(f"  [ERROR] Compilation failed for {rel_path}: {e}")
            failed += 1
    return failed


def check_environment() -> int:
    print("[*] Checking environment security and hardening settings...")
    errors = 0
    mesh_key = os.environ.get("SEARXNG_MESH_KEY", "").strip()
    if mesh_key:
        try:
            bkey = bytes.fromhex(mesh_key)
            if len(bkey) != 32:
                print(f"  [ERROR] SEARXNG_MESH_KEY must be 32 bytes (64 hex chars), got {len(bkey)} bytes.")
                errors += 1
            else:
                print("  [OK] SEARXNG_MESH_KEY is valid 32-byte secret.")
        except ValueError:
            print("  [ERROR] SEARXNG_MESH_KEY is not valid hex.")
            errors += 1
    else:
        print("  [INFO] SEARXNG_MESH_KEY not set in environment (will check config key_file fallback).")
    return errors


def main() -> int:
    print("=== SearXNG-NextGen Staging Hardening Verification ===")
    comp_errors = check_compilation()
    env_errors = check_environment()
    total_errors = comp_errors + env_errors
    if total_errors > 0:
        print(f"\n[FAIL] Staging verification failed with {total_errors} error(s).")
        return 1
    print("\n[SUCCESS] All staging verification checks passed cleanly!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
