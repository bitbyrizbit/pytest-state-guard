from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


def test_wheel_build_and_entrypoint_discovery():
    """Verify that the package builds cleanly as a wheel, installs into an
    isolated virtual environment, is auto-discovered via the pytest11 entry point,
    and can be natively disabled via '-p no:state_guard'.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    egg_info = repo_root / "src" / "pytest_state_guard.egg-info"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()

        try:
            # Step 1: Build wheel
            build_res = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", str(repo_root), "--no-deps", "-w", str(dist_dir)],
                capture_output=True,
                text=True,
            )
            assert build_res.returncode == 0, f"Wheel build failed: {build_res.stderr}\n{build_res.stdout}"
        finally:
            if egg_info.exists():
                import shutil
                shutil.rmtree(egg_info, ignore_errors=True)
            build_dir = repo_root / "build"
            if build_dir.exists():
                import shutil
                shutil.rmtree(build_dir, ignore_errors=True)

        wheels = list(dist_dir.glob("*.whl"))
        assert len(wheels) == 1, f"Expected 1 wheel, found {wheels}"
        wheel_file = wheels[0]

        # Step 2: Create isolated virtualenv with access to host pytest
        venv_dir = tmp_path / "venv"
        venv_res = subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
            capture_output=True,
            text=True,
        )
        assert venv_res.returncode == 0, f"Venv creation failed: {venv_res.stderr}"

        # Locate venv python
        if sys.platform == "win32":
            venv_python = venv_dir / "Scripts" / "python.exe"
        else:
            venv_python = venv_dir / "bin" / "python"
        assert venv_python.exists()

        # Step 3: Install wheel offline into isolated venv site-packages
        install_res = subprocess.run(
            [str(venv_python), "-m", "pip", "install", str(wheel_file), "--no-deps", "--force-reinstall"],
            capture_output=True,
            text=True,
        )
        assert install_res.returncode == 0, f"Pip install into venv failed: {install_res.stderr}\n{install_res.stdout}"

        # Step 4: Verify auto-discovery without -p flag
        test_file = tmp_path / "test_isolated.py"
        test_code = """
import threading
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

def test_leak_in_isolated_venv():
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(lambda: stop_event.wait(timeout=5))
"""
        test_file.write_text(test_code, encoding="utf-8")

        # Sanitize child environment so isolated consumer subprocess tests
        # real automatic entry-point discovery even if parent runner disables autoload
        child_env = os.environ.copy()
        child_env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)

        # Run pytest inside isolated venv WITHOUT -p flag:
        run_res = subprocess.run(
            [str(venv_python), "-m", "pytest", str(test_file)],
            capture_output=True,
            text=True,
            env=child_env,
        )
        # Must fail with exit code 1 because State Guard was auto-discovered and caught the leak!
        assert run_res.returncode == 1
        assert "ERROR at teardown of test_leak_in_isolated_venv" in run_res.stdout
        assert "State Guard Teardown Violation" in run_res.stdout

        # Step 5: Verify native deactivation via -p no:state_guard
        disable_res = subprocess.run(
            [str(venv_python), "-m", "pytest", "-p", "no:state_guard", str(test_file)],
            capture_output=True,
            text=True,
            env=child_env,
        )
        # Must succeed with exit code 0 because State Guard is disabled!
        assert disable_res.returncode == 0
        assert "1 passed" in disable_res.stdout
        assert "State Guard" not in disable_res.stdout
