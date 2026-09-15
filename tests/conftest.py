"""Shared pytest fixtures for the iSpindel dashboard tests.

These tests need:
1. An isolated, file-backed SQLite database (per-test) so the production DB
   is never touched.
2. Both an in-process FastAPI TestClient AND a live Uvicorn server (Phase 1
   behavior contracts must observe real network/HTTP behavior, not just the
   in-process shortcut).
3. A Chromium browser, falling back to the system chromium executable when
   Playwright's own browser isn't present.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def pytest_configure(config):
    """Register markers and bind the suite to explicit non-production mode."""
    os.environ["ISPINDEL_MODE"] = "test"
    config.addinivalue_line(
        "markers",
        "requires_browser: needs a real Chromium browser - auto-skipped if none available",
    )


# IMPORTANT: SQLITE_PATH must be set BEFORE app.main is imported, otherwise the
# module-level DB_PATH captures the default /data path. The fixture below does
# that and reloads the module per test for isolation.


def _free_loopback_port() -> int:
    """Bind 0.0.0.0:0 to pick a free ephemeral port on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def sqlite_tmp_path(tmp_path: Path) -> Path:
    """Allocate a unique SQLite file path under tmp_path."""
    p = tmp_path / "phase01-ispindel.db"
    p.touch()
    return p


@pytest.fixture
def app_module(monkeypatch, sqlite_tmp_path: Path):
    """Import (or reimport) app.main with SQLITE_PATH set to a temp file.

    The DB path is exported to the environment BEFORE the import so the
    module-level DB_PATH sees it, and ``importlib.reload`` is used so that
    module-level state (DB_PATH, init_db side effects) is rebuilt per test.
    """
    import importlib

    monkeypatch.setenv("SQLITE_PATH", str(sqlite_tmp_path))
    monkeypatch.setenv("BREW_SQLITE_PATH", str(sqlite_tmp_path.with_name("brew.db")))
    # Resolve imports from the dedicated repository root, independent of cwd.
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    if "app.main" in sys.modules:
        importlib.reload(sys.modules["app.main"])
    import app.main as app_main  # noqa: WPS433 - intentional fresh import

    return app_main


@pytest.fixture
def test_client(app_module):
    """In-process FastAPI TestClient bound to the freshly reloaded app."""
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as client:
        yield client


@pytest.fixture
def live_server(app_module):
    """Boot the app on a free loopback port via Uvicorn; readiness-polled.

    Yields a ``base_url`` like ``http://127.0.0.1:51234`` and shuts the server
    down cleanly on teardown.
    """
    from uvicorn import Config, Server

    port = _free_loopback_port()
    host = "127.0.0.1"
    base_url = f"http://{host}:{port}"

    config = Config(
        app_module.app,
        host=host,
        port=port,
        log_level="warning",
        lifespan="on",
        access_log=False,
    )
    server = Server(config)

    # Run server in a thread so the test stays synchronous.
    import threading

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Readiness poll: hit /health up to 30 times (≈ 3s) until the server responds.
    import httpx

    deadline = time.time() + 5.0
    ready = False
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=0.25) as probe:
                r = probe.get(f"{base_url}/health")
                if r.status_code == 200:
                    ready = True
                    break
        except Exception as e:  # noqa: BLE001 - probe loop
            last_err = e
            time.sleep(0.1)
    if not ready:
        server.should_exit = True
        thread.join(timeout=2)
        raise RuntimeError(
            f"uvicorn never became ready on {base_url}; last error: {last_err!r}"
        )

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def chromium_executable() -> str | None:
    """Path to a usable Chromium executable, or None if none is found.

    Order:
      1. ``/usr/bin/chromium`` (system package; checked first since it always works
         on this host and avoids downloading 100+ MB of Playwright browsers in CI).
      2. ``/usr/bin/chromium-browser``.
      3. The Playwright-managed chromium under ``PLAYWRIGHT_BROWSERS_PATH`` or the
         default cache locations.
    """
    candidates = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    # Fallback to Playwright's managed Chromium. Playwright stores them under
    # PLAYWRIGHT_BROWSERS_PATH, ~/.cache/ms-playwright, etc.
    pw_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    roots: list[Path] = []
    if pw_root:
        roots.append(Path(pw_root))
    roots.append(Path.home() / ".cache" / "ms-playwright")
    for root in roots:
        if not root.exists():
            continue
        for child in root.glob("chromium-*"):
            if child.is_dir():
                exe = child / "chrome-linux" / "chrome"
                if exe.exists():
                    return str(exe)
                exe2 = child / "chrome-linux64" / "chrome"
                if exe2.exists():
                    return str(exe2)
    return None


@pytest.fixture
def browser_page(chromium_executable: str | None, request):
    """Playwright Chromium page (function scope).

    Skips the test if no Chromium is found and the test is marked
    ``requires_browser``. Tests that run against TestClient/live_server but
    still assert on raw HTML need to use this fixture only when they truly
    need JS execution.
    """
    from playwright.sync_api import sync_playwright

    if chromium_executable is None:
        if request.node.get_closest_marker("requires_browser"):
            pytest.skip("No Chromium executable available")
        # If the test didn't ask for it, we still give back None so the test
        # can branch on it. But normally if you ask for the fixture, you want
        # a real browser; we raise here so misuses are loud.
        raise RuntimeError(
            "browser_page fixture requested but no Chromium executable is available"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, executable_path=chromium_executable
        )
        try:
            context = browser.new_context()
            page = context.new_page()
            yield page
        finally:
            browser.close()
