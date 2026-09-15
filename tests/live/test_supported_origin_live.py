"""Opt-in, read-only HTTPS browser checks for a supported origin.

The target is supplied by the operator because this module is deliberately not
an application/server fixture.  With any required setting absent, pytest
skips every node before browser setup and no network or credential-file access
occurs.
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest


_REQUIRED_SETTINGS = ("SUPPORTED_ORIGIN", "AUTH_USER", "AUTH_PASS_FILE")
_CONFIGURED = all(os.environ.get(name) for name in _REQUIRED_SETTINGS)
_SKIP_REASON = (
    "set SUPPORTED_ORIGIN, AUTH_USER, and AUTH_PASS_FILE to run the "
    "supported-origin live checks"
)

pytestmark = [
    pytest.mark.skipif(not _CONFIGURED, reason=_SKIP_REASON),
    pytest.mark.requires_browser,
]


class _ProbeFailure(AssertionError):
    """Failure containing only non-sensitive request targets and statuses."""


def _origin_from_environment() -> str:
    """Validate and return the configured HTTPS origin without credentials."""
    raw = os.environ.get("SUPPORTED_ORIGIN", "").strip()
    if not raw:
        raise _ProbeFailure("SUPPORTED_ORIGIN is empty")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise _ProbeFailure("SUPPORTED_ORIGIN is not a valid URL") from exc
    if parts.scheme.lower() != "https":
        raise _ProbeFailure("SUPPORTED_ORIGIN must use HTTPS")
    if not parts.netloc or parts.username is not None or parts.password is not None:
        raise _ProbeFailure("SUPPORTED_ORIGIN must contain only an HTTPS authority")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise _ProbeFailure("SUPPORTED_ORIGIN must be an origin, not a path or URL")
    return f"https://{parts.netloc}"


def _password_from_protected_file() -> str:
    """Read the Basic Auth password during fixture setup only.

    Symlinks, non-regular files, owner-unreadable files, and any group/other
    permission bits are rejected before the file is opened.
    """
    raw_path = os.environ.get("AUTH_PASS_FILE", "")
    if not raw_path:
        raise _ProbeFailure("AUTH_PASS_FILE is not configured")
    path = Path(raw_path)
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise _ProbeFailure("AUTH_PASS_FILE cannot be inspected") from exc
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or not (file_stat.st_mode & stat.S_IRUSR)
        or file_stat.st_mode & 0o077
        or path.is_symlink()
    ):
        raise _ProbeFailure(
            "AUTH_PASS_FILE must be a regular owner-readable file with no group/other permissions"
        )
    try:
        value = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _ProbeFailure("AUTH_PASS_FILE cannot be read as UTF-8") from exc
    if value.endswith("\n"):
        value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
    if not value:
        raise _ProbeFailure("AUTH_PASS_FILE must contain a non-empty password")
    return value


def _safe_target(url: str) -> str:
    """Return a path-only request target suitable for an assertion message."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return "/<invalid-url>"
    return path or "/"


def _same_origin(url: str, origin: str) -> bool:
    """Compare scheme and authority while never exposing URL userinfo."""
    try:
        actual = urlsplit(url)
        expected = urlsplit(origin)
    except ValueError:
        return False
    return (
        actual.scheme.lower(),
        actual.netloc.lower(),
    ) == (
        expected.scheme.lower(),
        expected.netloc.lower(),
    )


@dataclass
class _ReadOnlyProbe:
    """Browser event collection and read-only request guard."""

    origin: str
    page: Any
    failed_resources: list[str] = field(default_factory=list)
    non_2xx_gets: list[str] = field(default_factory=list)
    blocked_methods: list[str] = field(default_factory=list)

    def install_guards(self, context: Any) -> None:
        def guard_route(route: Any) -> None:
            request = route.request
            method = request.method.upper()
            if method not in {"GET", "HEAD"}:
                self.blocked_methods.append(method)
                route.abort()
                return
            if not _same_origin(request.url, self.origin):
                route.abort()
                return
            route.continue_()

        def record_failed(request: Any) -> None:
            if _same_origin(request.url, self.origin):
                self.failed_resources.append(_safe_target(request.url))

        def record_response(response: Any) -> None:
            request = response.request
            if (
                request.method.upper() == "GET"
                and _same_origin(response.url, self.origin)
                and not 200 <= response.status < 300
            ):
                self.non_2xx_gets.append(
                    f"{response.status} {_safe_target(response.url)}"
                )

        context.route("**/*", guard_route)
        self.page.on("requestfailed", record_failed)
        self.page.on("response", record_response)

    def assert_clean(self) -> None:
        problems: list[str] = []
        if self.blocked_methods:
            problems.append(f"blocked non-read methods: {self.blocked_methods!r}")
        if self.failed_resources:
            problems.append(f"failed same-origin resources: {self.failed_resources!r}")
        if self.non_2xx_gets:
            problems.append(f"non-2xx same-origin GETs: {self.non_2xx_gets!r}")
        if problems:
            raise _ProbeFailure("; ".join(problems))


def _settle(page: Any) -> None:
    """Give deferred asset and service-worker work a bounded settling window."""
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:  # Playwright timeout is version-specific; events remain useful.
        pass
    time.sleep(0.25)


def _load_json_with_browser(page: Any, path: str) -> dict[str, Any]:
    """Fetch JSON through the authenticated browser context using GET only."""
    result = page.evaluate(
        """async (path) => {
            const response = await fetch(path, {method: 'GET', credentials: 'same-origin'});
            return {status: response.status, body: await response.text()};
        }""",
        path,
    )
    if result["status"] != 200:
        raise _ProbeFailure(f"{path} returned HTTP {result['status']}")
    value = json.loads(result["body"])
    if not isinstance(value, dict):
        raise _ProbeFailure(f"{path} did not return a JSON object")
    return value


def _load_text_with_browser(page: Any, path: str) -> str:
    """Fetch text through the authenticated browser context using GET only."""
    result = page.evaluate(
        """async (path) => {
            const response = await fetch(path, {method: 'GET', credentials: 'same-origin'});
            return {status: response.status, body: await response.text()};
        }""",
        path,
    )
    if result["status"] != 200:
        raise _ProbeFailure(f"{path} returned HTTP {result['status']}")
    return result["body"]


def _assert_service_worker(page: Any, origin: str) -> None:
    deadline = time.monotonic() + 10.0
    registration: dict[str, str] | None = None
    while time.monotonic() < deadline:
        registration = page.evaluate(
            """async () => {
                const value = await navigator.serviceWorker.getRegistration('/');
                if (!value || !value.active || !value.active.scriptURL) return null;
                return {scope: value.scope, scriptURL: value.active.scriptURL};
            }"""
        )
        if registration:
            break
        time.sleep(0.1)
    if registration is None:
        raise _ProbeFailure("service worker did not become active within 10 seconds")
    scope = urlsplit(registration["scope"])
    script = urlsplit(registration["scriptURL"])
    if scope.path not in ("", "/"):
        raise _ProbeFailure("service worker scope is not the site root")
    if not _same_origin(registration["scriptURL"], origin):
        raise _ProbeFailure("service worker script is not same-origin")
    if script.path != "/static/service-worker.js":
        raise _ProbeFailure("unexpected service worker script path")


@pytest.fixture
def _supported_origin_config() -> tuple[str, str, str]:
    """Load all live configuration during test setup, never at collection."""
    origin = _origin_from_environment()
    username = os.environ.get("AUTH_USER", "")
    if not username:
        raise _ProbeFailure("AUTH_USER is empty")
    password = _password_from_protected_file()
    return origin, username, password


@pytest.fixture
def _readonly_browser(_supported_origin_config, chromium_executable):
    """Yield a browser page authenticated only by the setup-loaded password."""
    origin, username, password = _supported_origin_config
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=chromium_executable,
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            http_credentials={
                "username": username,
                "password": password,
                "origin": origin,
                "send": "always",
            },
        )
        page = context.new_page()
        probe = _ReadOnlyProbe(origin=origin, page=page)
        probe.install_guards(context)
        try:
            yield probe
        finally:
            context.close()
            browser.close()


def _open_origin(probe: _ReadOnlyProbe) -> None:
    """Open the origin and require the browser's ordinary HTTPS validation."""
    response = probe.page.goto(probe.origin + "/", wait_until="domcontentloaded")
    if response is None or not 200 <= response.status < 300:
        status = "missing" if response is None else str(response.status)
        raise _ProbeFailure(f"origin page returned HTTP {status}")
    if urlsplit(probe.page.url).scheme.lower() != "https":
        raise _ProbeFailure("browser did not remain on an HTTPS page")
    if not _same_origin(probe.page.url, probe.origin):
        raise _ProbeFailure("origin page redirected away from the configured origin")
    _settle(probe.page)


def _assert_tabs_render(probe: _ReadOnlyProbe) -> None:
    page = probe.page
    assert page.get_by_role("heading", name="Brewing Central").is_visible()
    expected = {
        "Dashboard": "#tab-dashboard",
        "Recipe Book": "#tab-recipes",
        "Brew Control & Archive": "#tab-brew",
    }
    for label, panel in expected.items():
        page.get_by_role("tab", name=label).click()
        assert page.locator(panel).is_visible(), label
    page.get_by_role("tab", name="Dashboard").click()


def test_supported_origin_returns_manifest_service_worker_and_tabs_with_tls_validation(
    _readonly_browser,
) -> None:
    """Validate HTTPS navigation, manifest, service worker, and all app tabs."""
    probe: _ReadOnlyProbe = _readonly_browser
    _open_origin(probe)
    manifest = _load_json_with_browser(probe.page, "/manifest.webmanifest")
    assert manifest.get("name") == "Brewing Central"
    assert manifest.get("scope") == "/"
    assert manifest.get("start_url")
    script = _load_text_with_browser(probe.page, "/static/service-worker.js")
    assert "addEventListener('fetch'" in script
    _assert_service_worker(probe.page, probe.origin)
    _assert_tabs_render(probe)
    probe.assert_clean()


def test_supported_origin_desktop_and_mobile_render_with_no_failed_resources(
    _readonly_browser,
) -> None:
    """Exercise the three read-only tabs at desktop and 320x568 viewports."""
    probe: _ReadOnlyProbe = _readonly_browser
    _open_origin(probe)
    _assert_tabs_render(probe)

    probe.page.set_viewport_size({"width": 320, "height": 568})
    _settle(probe.page)
    assert probe.page.viewport_size == {"width": 320, "height": 568}
    _assert_tabs_render(probe)
    assert probe.page.get_by_role("heading", name="Brewing Central").is_visible()
    probe.assert_clean()


def test_supported_origin_basic_auth_loaded_only_from_auth_pass_file(
    _readonly_browser,
) -> None:
    """Prove the setup-loaded file credential authenticates a read-only page."""
    probe: _ReadOnlyProbe = _readonly_browser
    _open_origin(probe)
    assert probe.page.get_by_role("heading", name="Brewing Central").is_visible()
    probe.assert_clean()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
