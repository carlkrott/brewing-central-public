"""HTTPS-origin secure-context / service-worker qualification.

Proves the app served over isolated HTTPS with a throwaway localhost
certificate exposes ``window.isSecureContext`` and registers
``/static/service-worker.js`` with scope ``/``. This is the secure-
context + service-worker branch only — it does NOT prove
authenticated Tailnet Caddy or production readiness.

No external network, no production state, no credentials. Bounded
waits. If Chromium or openssl is unavailable, FAIL LOUDLY (silent
skip is forbidden for the only test in this slice).
"""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest


SW_PATH = "/static/service-worker.js"
SW_SCOPE = "/"


def _require(name: str, fallback: str | None = None) -> str:
    path = shutil.which(name) or (fallback if fallback and Path(fallback).exists() else None)
    if not path:
        pytest.fail(f"required tool '{name}' is not installed; silent skip is forbidden")
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(url: str, deadline_s: float = 5.0) -> None:
    end = time.time() + deadline_s
    last: Exception | None = None
    while time.time() < end:
        try:
            with httpx.Client(timeout=0.5, verify=False) as probe:
                if probe.get(f"{url}/health").status_code == 200:
                    return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.1)
    pytest.fail(f"HTTPS app never became ready at {url}/health: last={last!r}")


@pytest.mark.requires_browser
def test_localhost_https_secure_context_and_service_worker(
    app_module, tmp_path: Path, chromium_executable: str | None,
) -> None:
    """Localhost TLS proves the secure-context + service-worker branch only.

    Honest boundary: does NOT prove authenticated Tailnet Caddy or
    production readiness.
    """
    if chromium_executable is None:
        pytest.fail(
            "Chromium not available; the only test in this slice cannot skip"
        )
    openssl_bin = _require("openssl", "/usr/bin/openssl")

    cert = tmp_path / "localhost.crt"
    key = tmp_path / "localhost.key"
    subprocess.run([
        openssl_bin, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ], check=True, capture_output=True, timeout=15)

    host = "127.0.0.1"
    port = _free_port()
    base = f"https://{host}:{port}"

    from uvicorn import Config, Server

    server = Server(Config(
        app_module.app, host=host, port=port, log_level="warning",
        lifespan="on", access_log=False,
        ssl_keyfile=str(key), ssl_certfile=str(cert),
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    page_errs: list[str] = []
    cons_errs: list[str] = []
    browser = context = page = None
    try:
        _wait_ready(base)
        # Confirm TLS terminated. Cert is self-signed so we disable chain
        # checks. No certificate content is printed.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=2) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                assert ss.version() in {"TLSv1.2", "TLSv1.3"}

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, executable_path=chromium_executable,
                # Service-worker fetches under a self-signed cert are
                # rejected even with ignore_https_errors=True.
                args=["--no-sandbox", "--ignore-certificate-errors"],
            )
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()
            page.on("console", lambda m: cons_errs.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: page_errs.append(str(e)))
            page.goto(f"{base}/", wait_until="domcontentloaded")

            assert page.evaluate("() => window.isSecureContext") is True, (
                "HTTPS localhost must yield window.isSecureContext === true"
            )

            # Bounded poll for service worker registration.
            info: dict[str, str] | None = None
            deadline = time.time() + 10.0
            while time.time() < deadline:
                info = page.evaluate(
                    """async () => {
                        const r = await navigator.serviceWorker.getRegistration('/');
                        if (!r || !r.active || !r.active.scriptURL) return null;
                        return {scope: r.scope, scriptURL: r.active.scriptURL};
                    }"""
                )
                if info:
                    break
                time.sleep(0.1)
            assert info is not None, "service worker did not register within 10s"
            scope_path = urlsplit(info["scope"]).path or "/"
            assert scope_path.rstrip("/") == SW_SCOPE.rstrip("/"), info["scope"]
            assert info["scriptURL"].endswith(SW_PATH), info["scriptURL"]
            assert cons_errs == [], f"console errors: {cons_errs}"
            assert page_errs == [], f"page errors: {page_errs}"
    finally:
        for obj in (page, context, browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        server.should_exit = True
        thread.join(timeout=5)
