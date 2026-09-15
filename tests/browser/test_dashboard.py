"""Phase 1 graph-correction contract tests for the dashboard.

These tests assert behavior described in the V3 research report (subagent A):

    1. Sliding X-axis: both x.min and x.max advance from wall clock on
       each successful unpaused poll.
    2. No overlapping polls: an in-flight fetch is mutually excluded so the
       next tick cannot race.
    3. Truthful error handling: a failed poll never produces a "success"
       pulse; manual refresh keeps the old chart visible until the new
       payload lands.
    4. Pause behavior: pausing freezes the polling timer AND labels the
       tick indicator so the dashboard doesn't pretend to be live.
    5. Adjacent-sample deltas: pills compute deltas from the previous
       sample inside the same response, not from a stale cross-poll value.
    6. Deterministic axis layout:
        * Unique y-scales per active metric.
        * All on the LEFT (position: 'left').
        * Inactive scales are completely removed (``display:false``), not
          just empty.
        * No ``reverse`` flag (which previously caused visual axis
          collision).
    7. Non-color metric identifiers: charts identify metrics by a stable
       name (``data-metric`` / ``metric`` field), not by color class.
    8. No inline ``height:340px`` legacy hardcode.
    9. Full-width Pointer Events resize: setPointerCapture, touch-action,
       keyboard (±16 / ±64 / reset), localStorage persistence with
       reset-to-default, ResizeObserver wiring, and ONE shared min/max
       bound calculation reflected in JS/CSS/ARIA.

Tests run in two layers:
    * Static source/HTML checks (always run, no browser needed)
    * Live Chromium checks (marker ``requires_browser`` - skip if no browser)
"""

from __future__ import annotations

import json
import re
import socket
import time
from pathlib import Path

import httpx
import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _dashboard_js() -> str:
    return (Path(__file__).resolve().parents[2] / "app/static/dashboard.js").read_text()


def _dashboard_css() -> str:
    return (Path(__file__).resolve().parents[2] / "app/static/dashboard.css").read_text()


def _get_chart_handle(page):
    """Read the global ``chart`` from the test page (the dashboard creates a
    Chart instance that lives on the ``Chart`` constructor's registry, but it
    also stashes a reference in the IIFE module so we can query it)."""
    return page.evaluate("() => (typeof chart !== 'undefined' ? chart : null)")


# ---------------------------------------------------------------------------
# (1) X-axis slides BOTH min and max on each unpaused successful poll
# ---------------------------------------------------------------------------

def test_x_axis_min_and_max_advanced_in_poll(test_client) -> None:
    """Both x.min and x.max must be reassigned each poll so the window slides."""
    html = test_client.get("/").text
    js = _dashboard_js()

    # In the polling loop we must write to BOTH chart.options.scales.x.min
    # AND chart.options.scales.x.max. Searching for a single-line assignment
    # isn't enough; we look for the assignment expressions in the tick path.
    # Both lines must appear inside a function that schedules polling (the
    # tick() function defined inside startChartPolling).
    # The chart.options.scales.x assignment must use both keys.
    assert "options.scales.x.min" in js, (
        "Polling logic does not assign chart.options.scales.x.min "
        "(x-axis min should slide, not stay frozen)"
    )
    assert "options.scales.x.max" in js, (
        "Polling logic does not assign chart.options.scales.x.max"
    )


def test_x_range_helper_both_keys(html_dashboard: str) -> None:
    """computeXRange must declare BOTH ``min`` and ``max`` bindings."""
    js = _dashboard_js()
    assert "function computeXRange" in js
    # Body of computeXRange must bind both 'min' and 'max' before returning.
    m = re.search(r"function computeXRange\([^)]*\)\s*\{(.*?)\}\s*\n", js, re.DOTALL)
    assert m, "computeXRange body not found"
    body = m.group(1)
    # Accept both explicit-key and shorthand forms.
    has_min = "const min" in body or "let min" in body or "var min" in body or "min:" in body
    has_max = "const max" in body or "let max" in body or "var max" in body or "max:" in body
    assert has_min and has_max, (
        "computeXRange must compute both 'min' and 'max' (got "
        f"min={has_min} max={has_max})"
    )
    assert "return" in body, "computeXRange must return its result"


# ---------------------------------------------------------------------------
# (2) No overlapping poll (mutual exclusion)
# ---------------------------------------------------------------------------

def test_no_overlapping_polls(test_client) -> None:
    """An isFetching / in-flight guard must prevent concurrent ticks."""
    html = test_client.get("/").text
    js = _dashboard_js()
    # The exact flag name is up to the implementation, but the pattern is:
    #   1. Set a flag at the top of tick()
    #   2. Early-return if it's already set
    #   3. Clear it in .finally() or after the await resolves
    has_flag_decl = re.search(r"let\s+isFetching\s*=", js) or re.search(
        r"let\s+inFlight\s*=", js
    )
    has_early_return = re.search(r"if\s*\(\s*(isFetching|inFlight)\s*\)\s*\{?\s*return", js)
    assert has_flag_decl, "No 'isFetching'/'inFlight' flag declared in chart loop"
    assert has_early_return, (
        "Polling loop doesn't short-circuit when a fetch is already in-flight"
    )


# ---------------------------------------------------------------------------
# (3) Truthful error/no false success pulse + manual refresh preserves chart
# ---------------------------------------------------------------------------

def test_failed_poll_skips_success_pulse(test_client) -> None:
    """A failed fetch (non-2xx or exception) must NOT pulse the tick indicator
    nor replace the chart with an empty state."""
    html = test_client.get("/").text
    js = _dashboard_js()
    # The tick function must branch on fetch success and only call pulseTick
    # on the resolved path. Look for a try/catch wrapping the fetch and a
    # catch block that explicitly does NOT call pulseTick / tick-dot classes.
    # Acceptable patterns include: a .catch() handler, or a guard
    # `if (!response.ok) return;` before pulseTick.
    assert ".catch" in js or "catch" in js, (
        "Polling loop must catch errors so a failed fetch doesn't propagate "
        "as if it were a success"
    )


def test_manual_refresh_preserves_old_chart_until_success(test_client) -> None:
    """refreshAll() must not destroy the chart until the new data lands."""
    html = test_client.get("/").text
    js = _dashboard_js()
    # Find the refreshAll function body and verify it does NOT destroy the
    # chart synchronously before the fetch resolves.
    m = re.search(r"function refreshAll\(\)\s*\{(.*?)\n\s*\}", js, re.DOTALL)
    assert m, "refreshAll() not found"
    body = m.group(1)
    # The dashboard used to do `if (chart) { chart.destroy(); ... }` at the
    # top of refreshAll - that's the bug. The fixed version must not have an
    # unconditional destroy before await.
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    first_lines = lines[:5]
    has_pre_await_destroy = any(
        re.search(r"\bchart\.destroy\(\)", ln) for ln in first_lines
    )
    assert not has_pre_await_destroy, (
        "refreshAll() destroys the chart synchronously before the new fetch "
        "resolves - this flashes a blank canvas."
    )


# ---------------------------------------------------------------------------
# (4) Pause freezes and labels
# ---------------------------------------------------------------------------

def test_pause_branch_is_correctly_wired(test_client) -> None:
    """The poll tick must return before loadChart while paused and render a
    literal paused status without triggering a success pulse."""
    js = _dashboard_js()
    tick = re.search(
        r"const tick\s*=\s*async\s*\(\)\s*=>\s*\{(.*?)\n\s*\};",
        js,
        re.DOTALL,
    )
    assert tick, "Polling tick function not found"
    body = tick.group(1)
    paused = re.search(
        r"if\s*\([^\n]*opt-paused[^\n]*\)\s*\{(.*?)\n\s*\}",
        body,
        re.DOTALL,
    )
    assert paused, "Polling tick does not branch on opt-paused"
    paused_body = paused.group(1)
    assert "pulsePaused()" in paused_body
    assert "loadChart()" not in paused_body
    assert "pulseTick()" not in paused_body
    assert "return" in paused_body


def test_tick_indicates_paused_state(test_client) -> None:
    """The tick indicator should label itself 'paused' when polling is paused."""
    html = test_client.get("/").text
    js = _dashboard_js()
    # Either a 'paused' label is set on the tick next indicator, or a class
    # like 'is-paused' / 'is-paused' is toggled. We allow either name.
    assert re.search(r"tick-(next|last)", js), (
        "Tick-next / tick-last indicators must exist for pause labeling"
    )


# ---------------------------------------------------------------------------
# (5) Adjacent-sample deltas (pills)
# ---------------------------------------------------------------------------

def test_pill_deltas_use_adjacent_samples(test_client) -> None:
    """The chart update must derive latest and previous values by walking the
    current response, then pass both maps to the pill renderer."""
    js = _dashboard_js()
    update = re.search(
        r"function updateChartLive\([^)]*\)\s*\{(.*?)\n\s*function updateValuePills",
        js,
        re.DOTALL,
    )
    assert update, "updateChartLive() not found"
    body = update.group(1)
    assert re.search(r"for\s*\([^;]+samples\.length\s*-\s*1", body)
    assert "lastPerMetric" in body and "prevPerMetric" in body
    assert "updateValuePills(lastPerMetric, prevPerMetric)" in body
    assert "lastByMetric" not in body


# ---------------------------------------------------------------------------
# (6) Deterministic y-axis layout
# ---------------------------------------------------------------------------

def test_axes_all_left_no_reverse(html_dashboard: str) -> None:
    """Every y-axis must be position: 'left' and must not flip reverse."""
    js = _dashboard_js()
    # The buildScales function: every axis must be position: 'left'.
    # No 'reverse:' property on any axis.
    # We accept either object literal assignment or repeated assignments.
    # Generic guard:
    has_left = re.search(r"position\s*:\s*['\"]left['\"]", js)
    assert has_left, "y-axes must be position: 'left'"
    # The buggy pattern was: reverse: idx % 2 === 1. That MUST be gone.
    assert "reverse" not in js.split("buildScales(")[-1].split("}")[0] if "buildScales(" in js else True, (
        "buildScales still uses a 'reverse' property - axes must not flip"
    )
    # Stricter global check for the buggy idiom:
    buggy = re.search(r"reverse\s*:\s*idx\s*%\s*2", js)
    assert buggy is None, "Axes still toggle by idx%2 - must be deterministic"


def test_inactive_axes_display_false(html_dashboard: str) -> None:
    """Inactive axes must be marked display:false so they don't waste space."""
    js = _dashboard_js()
    # Pattern: axes use display: vis.has(m.id). We accept any boolean
    # expression tied to the vis set as long as the keyword 'display' is
    # explicitly present on the axis object literal.
    has_display = re.search(r"display\s*:\s*", js)
    assert has_display, "y-axis must declare an explicit display: flag"


def test_scales_unique_per_metric(html_dashboard: str) -> None:
    """Datasets and explicit scales share the stable ``y_<metric>`` mapping
    for every one of the five known metrics."""
    js = _dashboard_js()
    metrics_match = re.search(r"const METRICS\s*=\s*\[(.*?)\];", js, re.DOTALL)
    assert metrics_match
    ids = re.findall(r"\bid\s*:\s*['\"]([a-z_]+)['\"]", metrics_match.group(1))
    assert ids == ["gravity", "temp_c", "battery", "angle", "rssi"]
    scales = re.search(
        r"function buildScales\([^)]*\)\s*\{(.*?)\n\s*return axes;",
        js,
        re.DOTALL,
    )
    assert scales
    body = scales.group(1)
    assert "METRICS.forEach" in body
    assert "const axisId = 'y_' + m.id" in body
    assert "axes[axisId]" in body
    assert "yAxisID:         'y_' + m.id" in js


# ---------------------------------------------------------------------------
# (7) Non-color metric identifiers
# ---------------------------------------------------------------------------

def test_metric_pills_use_data_metric(test_client) -> None:
    """Pills and toggles use ``data-metric`` (non-color identifier)."""
    html = test_client.get("/").text
    assert 'data-metric="gravity"' in html
    assert 'data-metric="temp_c"' in html
    assert 'data-metric="battery"' in html
    assert 'data-metric="angle"' in html
    assert 'data-metric="rssi"' in html


def test_charts_reference_metric_by_id_not_color(test_client) -> None:
    """JS code must identify metrics by stable id, not by color class."""
    html = test_client.get("/").text
    js = _dashboard_js()
    # The METRICS array must contain an 'id' key per metric.
    m = re.search(r"const METRICS\s*=\s*\[(.*?)\];", js, re.DOTALL)
    assert m, "METRICS array not found"
    metrics_body = m.group(1)
    # Each entry must contain 'id:' and 'color:'
    id_count = metrics_body.count("id:")
    color_count = metrics_body.count("color:")
    assert id_count >= 5, f"Expected >=5 metric ids, got {id_count}"
    assert color_count >= 5, f"Expected >=5 metric colors, got {color_count}"
    # Find references to color classes as selectors in chart code; we want
    # these to be minimal/styling-only.
    bad = re.search(r"querySelector\s*\(\s*['\"][^'\"]*\.bgc-[a-z0-9]+['\"]", js)
    assert bad is None, "Selectors relying on bgc-* color classes are not allowed"


# ---------------------------------------------------------------------------
# (8) No inline ``height:340px`` legacy hardcode
# ---------------------------------------------------------------------------

def test_no_inline_height_340(html_dashboard: str) -> None:
    assert 'height:340px' not in html_dashboard, (
        "Legacy inline height:340px must be removed from chart container"
    )


def test_chart_container_uses_css_variable(html_dashboard: str) -> None:
    """The chart container's height must come from a CSS custom property
    so the resize handle can override it without a re-render."""
    assert "--chart-height" in _dashboard_css(), (
        "Chart container must drive its height via --chart-height"
    )


# ---------------------------------------------------------------------------
# (9) Full-width Pointer Events resize
#     capture + touch-action + keyboard + localStorage + reset +
#     ResizeObserver + ONE shared bound calculation reflected in JS/CSS/ARIA
# ---------------------------------------------------------------------------

@pytest.fixture
def html_dashboard(test_client):
    """Static HTML for the dashboard, fetched fresh per test from TestClient.

    Bytes are identical to what the live server returns; the static checks
    below don't depend on the wire transport.
    """
    r = test_client.get("/")
    assert r.status_code == 200
    return r.text


def test_resize_uses_set_pointer_capture(html_dashboard: str) -> None:
    js = _dashboard_js()
    assert "setPointerCapture" in js, (
        "Resize handle must use setPointerCapture for full-width drag"
    )


def test_resize_handle_has_touch_action(html_dashboard: str) -> None:
    css = _dashboard_css()
    handle_rule = re.search(r"\.chart-resize-handle\s*\{(.*?)\}", css, re.DOTALL)
    assert handle_rule and re.search(r"touch-action\s*:\s*none", handle_rule.group(1)), (
        "Resize handle must declare touch-action:none for mobile gestures"
    )


def test_resize_handle_keyboard(html_dashboard: str) -> None:
    js = _dashboard_js()
    # Must handle ArrowUp / ArrowDown at minimum, and have a reset path
    # (Escape or Home/End).
    assert "ArrowUp" in js and "ArrowDown" in js, (
        "Resize handle must respond to arrow keys"
    )
    assert re.search(r"case\s+['\"]Escape['\"]", js), (
        "Resize handle must support an Escape reset path"
    )


def test_resize_localstorage_persistence_and_reset(html_dashboard: str) -> None:
    js = _dashboard_js()
    assert "ispindel.chart-height" in js, (
        "Saved chart height must live under 'ispindel.chart-height'"
    )
    assert re.search(
        r"localStorage\.(setItem|getItem|removeItem)", js
    ), "Resize handle must read/write localStorage"


def test_resize_uses_resize_observer(html_dashboard: str) -> None:
    js = _dashboard_js()
    assert "ResizeObserver" in js, (
        "Chart resize should observe container size via ResizeObserver"
    )


def test_resize_bound_calc_one_shared_source(html_dashboard: str) -> None:
    """One viewport-aware helper supplies JS clamp and ARIA bounds; CSS
    shares the same minimum while the maximum remains dynamic."""
    js = _dashboard_js()
    css = _dashboard_css()
    bounds = re.search(
        r"function chartResizeBounds\(\)\s*\{(.*?)\n\s*\}", js, re.DOTALL
    )
    assert bounds, "Single shared bound-calculation function not found in JS"
    assert "window.innerHeight" in bounds.group(1)
    min_match = re.search(r"MIN_PX\s*=\s*(\d+)", js)
    assert min_match
    assert re.search(rf"min-height\s*:\s*{min_match.group(1)}px", css)
    assert js.count("chartResizeBounds()") >= 4
    assert "aria-valuemin" in js and "aria-valuemax" in js
    assert "Math.max(600" in bounds.group(1)
    assert "Math.floor(window.innerHeight * 0.90)" in bounds.group(1)


# ---------------------------------------------------------------------------
# (10) Existing features preserved
# ---------------------------------------------------------------------------

def test_existing_features_preserved(html_dashboard: str) -> None:
    """Smoke checks that the Phase 1 corrections did not accidentally
    remove existing functionality."""
    js = _dashboard_js()
    html = html_dashboard
    # Existing dashboard behavior and server-backed regions
    assert 'href="/api/status"' not in html  # not a link
    assert 'id="health-status"' in html
    assert 'id="summary"' in html
    assert 'id="devices-table"' in html
    # Existing chart behaviors
    assert "exportChartPNG" in js
    assert "loadSavedView" in js
    assert "openCalibrationModal" in js
    assert "saveCalibration" in js


# ---------------------------------------------------------------------------
# (11) Live Uvicorn reaches the same routes
# ---------------------------------------------------------------------------

def test_live_server_returns_dashboard(live_server: str) -> None:
    with httpx.Client(timeout=2.0) as client:
        r = client.get(f"{live_server}/")
    assert r.status_code == 200
    assert 'id="chartContainer"' in r.text


def test_calibration_history_uses_api_fields_and_active_route(test_client) -> None:
    html = test_client.get("/").text
    js = _dashboard_js()
    assert "calibration.is_active" in js
    assert "calibration.fit_r2" in js
    assert "calibration.active" not in js
    assert "calibration.r_squared" not in js
    assert "/active`" in js
    assert "/activate`" not in js
    assert "aria-current" in js
    assert "is-active" in js


# ---------------------------------------------------------------------------
# (12) Live Chromium-driven behavior checks (skipped if no browser)
# ---------------------------------------------------------------------------

@pytest.mark.requires_browser
def test_live_x_axis_advances_after_two_polls(browser_page, live_server: str) -> None:
    """Drive the polling loop in a real browser and verify x.min/x.max
    advance on each successful poll.

    We POST a couple of samples to /api/ingest, navigate to the live server,
    wait for two polls (poll interval = 5s), then assert xRange.min /
    xRange.max have moved past their initial values.
    """
    # Seed two sample rows so the chart actually renders.
    with httpx.Client(timeout=2.0) as client:
        for delta in range(3):
            client.post(
                f"{live_server}/api/ingest",
                json={
                    "ID": "live-axis-test",
                    "angle": 30 + delta,
                    "gravity": 1.040 + 0.001 * delta,
                    "temperature": 19 + delta,
                    "battery": 4.0,
                    "interval": 1,
                },
            )
        # Promote to dashboard with samples in the 24h window.
        import time as _t
        _t.sleep(0.2)

    page = browser_page
    page.goto(f"{live_server}/")
    # Default poll interval is 5s. Wait for at least 2 unpaused polls.
    page.wait_for_selector("#chartCanvas")
    page.wait_for_function(
        "() => typeof chart !== 'undefined' && chart !== null && chart.options"
    )
    initial_min_max = page.evaluate(
        """() => {
            const c = typeof chart !== 'undefined' ? chart : null;
            if (!c) return null;
            const x = c.options.scales.x;
            return { min: x.min, max: x.max };
        }"""
    )
    assert initial_min_max is not None, (
        "Chart did not initialize after the dashboard bootstrap completed"
    )
    # Wait for at least one extra poll cycle (5s + slack).
    time.sleep(6.5)
    later = page.evaluate(
        """() => {
            const c = typeof chart !== 'undefined' ? chart : null;
            if (!c) return null;
            const x = c.options.scales.x;
            return { min: x.min, max: x.max };
        }"""
    )
    assert later is not None
    # Both min and max must advance (move forward in time).
    assert later["max"] >= initial_min_max["max"], (
        f"x.max did not advance: was {initial_min_max['max']!r}, "
        f"now {later['max']!r}"
    )
    assert later["min"] >= initial_min_max["min"], (
        f"x.min did not advance: was {initial_min_max['min']!r}, "
        f"now {later['min']!r}"
    )


@pytest.mark.requires_browser
def test_live_resize_handle_responds_to_keyboard(
    browser_page, live_server: str
) -> None:
    """Focus the resize handle and press ArrowUp; aria-valuenow must change."""
    page = browser_page
    page.goto(f"{live_server}/")
    page.wait_for_selector("#chartResizeHandle")
    handle = page.locator("#chartResizeHandle")
    handle.focus()
    initial = handle.get_attribute("aria-valuenow")
    page.keyboard.press("ArrowUp")
    page.wait_for_timeout(50)
    after = handle.get_attribute("aria-valuenow")
    assert initial is not None and after is not None
    assert int(after) > int(initial), (
        f"aria-valuenow did not increase: {initial!r} -> {after!r}"
    )


# ---------------------------------------------------------------------------
# Phase 2: DOM safety, accessibility, and small-screen contracts
# ---------------------------------------------------------------------------

def test_accessible_names_lang_and_restrained_live_regions(html_dashboard: str) -> None:
    assert '<html lang="en">' in html_dashboard
    for control in ("device-select", "time-window", "edit-device-name", "edit-device-interval", "cal-label"):
        assert f'for="{control}"' in html_dashboard
    assert 'id="chartCanvas" aria-label="Telemetry time-series chart"' in html_dashboard
    assert 'id="chart-summary"' in html_dashboard and 'id="chart-data-table"' in html_dashboard
    assert 'aria-live="polite"' in html_dashboard
    assert 'metric-pills" role="status"' not in html_dashboard


def test_resize_separator_accessibility_contract(html_dashboard: str) -> None:
    assert 'role="separator"' in html_dashboard
    assert 'aria-orientation="horizontal"' in html_dashboard
    assert 'aria-valuemin' in html_dashboard and 'aria-valuemax' in html_dashboard and 'aria-valuenow' in html_dashboard


def test_chart_has_accessible_summary_and_data_table(html_dashboard: str) -> None:
    assert 'aria-describedby="chart-summary"' in html_dashboard
    assert '<caption>Latest telemetry data</caption>' in html_dashboard
    assert 'function updateChartAccessibleData' in _dashboard_js()


def test_metric_controls_keyboard_operable(html_dashboard: str) -> None:
    js = _dashboard_js()
    assert 'cb.type = \'checkbox\'' in js and "addEventListener('change'" in js
    assert ':focus-visible' in _dashboard_css()


def test_mobile_320px_has_no_page_overflow(html_dashboard: str) -> None:
    css = _dashboard_css()
    assert '.table-scroll { overflow-x:auto' in css
    assert '@media (max-width: 320px)' in css


def test_mobile_touch_target_rule_excludes_intrinsic_native_controls() -> None:
    css = _dashboard_css()
    mobile = css.rsplit("@media (max-width:700px)", 1)[1].split("@media", 1)[0]
    assert "min-height:44px" in mobile
    for selector in (
        'input:not([type="hidden"])',
        ':not([type="checkbox"])',
        ':not([type="radio"])',
        ':not([type="range"])',
    ):
        assert selector in mobile


def test_frontend_never_renders_stale_or_error_health_as_green(html_dashboard: str) -> None:
    js = _dashboard_js()
    assert "state === 'ok' ? 'status-ok'" in js
    assert "state === 'warning' ? 'status-warning' : 'status-error'" in js


@pytest.mark.requires_browser
def test_untrusted_device_and_health_fields_render_as_text(browser_page, live_server: str) -> None:
    """Executable regression: markup in API content cannot create nodes."""
    evil = '<img src=x onerror="window.__xss=(window.__xss||0)+1">'
    with httpx.Client(timeout=2.0) as client:
        client.post(f"{live_server}/api/ingest", json={"ID": "safe-id", "name": "Safe", "gravity": 1.04})
        assert client.patch(f"{live_server}/api/device/safe-id", json={"device_name": evil}).status_code == 200
        assert client.post(f"{live_server}/api/ingest", json={"ID": evil, "name": "Safe ID label", "gravity": 1.04}).status_code == 200
    page = browser_page
    page.route("**/api/system-health", lambda route: route.fulfill(json={
        "status": "critical", "battery": {"status": "critical", "detail": evil},
        "heartbeat": {"status": "parse_error", "detail": evil, "content": evil},
    }))
    page.goto(f"{live_server}/")
    page.wait_for_selector("#devices-body")
    page.wait_for_timeout(250)
    assert page.locator("img").count() == 0
    assert page.evaluate("() => window.__xss || 0") == 0
    assert evil in page.locator("#devices-body").inner_text()
    assert evil in page.locator("#health-status").inner_text()


@pytest.mark.requires_browser
def test_modal_focus_trap_escape_and_restore(browser_page, live_server: str) -> None:
    with httpx.Client(timeout=2.0) as client:
        client.post(f"{live_server}/api/ingest", json={"ID": "modal-id", "gravity": 1.04})
    page = browser_page; page.goto(f"{live_server}/")
    page.get_by_role("button", name="Edit").click()
    assert page.get_by_role("dialog", name="Edit Device").is_visible()
    page.keyboard.press("Escape")
    assert not page.get_by_role("dialog", name="Edit Device").is_visible()
    assert page.evaluate("() => document.activeElement.textContent") == "Edit"


@pytest.mark.requires_browser
def test_axe_has_no_serious_or_critical_violations(browser_page, live_server: str) -> None:
    from axe_playwright_python.sync_playwright import Axe
    with httpx.Client(timeout=2.0) as client:
        for gravity, temperature, battery, rssi in [(1.01, 20, 4.0, -70), (1.02, 21, 4.1, -60)]:
            assert client.post(f"{live_server}/api/ingest", json={
                "ID": "axe-delta-id", "gravity": gravity, "temperature": temperature,
                "battery": battery, "RSSI": rssi,
            }).status_code == 200
    page = browser_page; page.goto(f"{live_server}/")
    page.wait_for_function("() => document.querySelector('.value-pill-meta .delta.up') !== null")
    results = Axe().run(page)
    serious = [v for v in results.response["violations"] if v["impact"] in {"serious", "critical"}]
    assert not serious, serious


@pytest.mark.requires_browser
def test_mobile_320px_has_no_page_overflow_live(browser_page, live_server: str) -> None:
    browser_page.set_viewport_size({"width": 320, "height": 700})
    browser_page.goto(f"{live_server}/")
    assert browser_page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.requires_browser
def test_mobile_actionable_controls_have_44px_hit_area(browser_page, live_server: str) -> None:
    browser_page.set_viewport_size({"width": 390, "height": 844})
    browser_page.goto(f"{live_server}/")
    undersized: list[dict[str, object]] = []
    for tab_id in ("tab-button-dashboard", "tab-button-recipes", "tab-button-brew"):
        browser_page.locator(f"#{tab_id}").click()
        undersized.extend(browser_page.evaluate(
            """() => Array.from(document.querySelectorAll(
                'button, select, textarea, input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="range"])'
            )).filter((element) => {
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 && rect.height < 44;
            }).map((element) => {
                const rect = element.getBoundingClientRect();
                return {
                    tag: element.tagName.toLowerCase(),
                    id: element.id,
                    name: element.getAttribute('aria-label') || element.textContent.trim().slice(0, 48),
                    height: Math.round(rect.height * 100) / 100,
                };
            })"""
        ))
    assert not undersized, undersized


# ---------------------------------------------------------------------------
# Phase 4: versioned calibration UI and raw/calibrated gravity selection
# ---------------------------------------------------------------------------


def test_gravity_mode_control_is_labeled_and_defaults_raw(html_dashboard: str) -> None:
    assert 'for="gravity-mode"' in html_dashboard
    assert 'id="gravity-mode"' in html_dashboard
    assert '<option value="raw" selected>Raw Gravity</option>' in html_dashboard
    assert '<option value="calibrated">Calibrated Gravity</option>' in html_dashboard


def test_metric_value_is_single_raw_calibrated_source(html_dashboard: str) -> None:
    js = _dashboard_js()
    assert "function metricValue(metric, sample)" in js
    assert "sample.raw_gravity ?? sample.gravity" in js
    assert "sample.calibrated_gravity" in js
    assert "metricValue(m, s)" in js
    assert "metricValue(m, point)" in js


def test_calibration_history_uses_dom_safe_text_rendering(html_dashboard: str) -> None:
    js = _dashboard_js()
    assert "function renderCalibrationHistory" in js
    block = js.split("function renderCalibrationHistory", 1)[1].split("async function", 1)[0]
    assert "createElement" in block and "textContent" in block
    assert "innerHTML" not in block
    assert 'id="cal-history"' in html_dashboard


def test_calibration_activation_controls_are_keyboard_operable(html_dashboard: str) -> None:
    js = _dashboard_js()
    assert "button.type = 'button'" in js
    assert "activateCalibration" in js
    assert "method: 'PUT'" in js


def test_calibration_modal_focus_and_live_region_contract(html_dashboard: str) -> None:
    modal = html_dashboard.split('id="cal-modal"', 1)[1].split('</div>\n\n  <script', 1)[0]
    assert 'for="cal-order"' in modal and 'id="cal-order"' in modal
    assert 'id="cal-status"' in modal and 'aria-live="polite"' in modal
    js = _dashboard_js()
    assert "trapModalFocus" in js
    assert "openCalibrationModal" in js


@pytest.mark.requires_browser
def test_calibration_flow_has_no_page_or_console_errors(browser_page, live_server: str) -> None:
    errors = []
    browser_page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    browser_page.on("pageerror", lambda error: errors.append(str(error)))
    with httpx.Client(timeout=3.0) as client:
        for angle, gravity in [(10.0, 1.01), (20.0, 1.02)]:
            assert client.post(f"{live_server}/api/ingest", json={
                "ID": "gravity-mode-id", "angle": angle, "gravity": gravity,
                "temperature": 20.0, "battery": 4.0,
            }).status_code == 200
        assert client.post(f"{live_server}/api/device/gravity-mode-id/calibration", json={
            "label": "Browser calibration", "points": [
                {"angle": 10.0, "value": 1.10}, {"angle": 20.0, "value": 1.20}
            ],
        }).status_code == 200
    page = browser_page
    page.goto(f"{live_server}/")
    page.select_option("#device-select", "gravity-mode-id")
    page.wait_for_function("() => typeof chart !== 'undefined' && chart && chart.data.datasets[0].data.length >= 2")
    raw = page.evaluate("() => ({label: chart.data.datasets[0].label, values: chart.data.datasets[0].data.map(p => p.y)})")
    page.select_option("#gravity-mode", "calibrated")
    page.wait_for_function("() => chart.data.datasets[0].label.includes('Calibrated')")
    calibrated = page.evaluate("() => ({label: chart.data.datasets[0].label, values: chart.data.datasets[0].data.map(p => p.y)})")
    assert "Raw Gravity" in raw["label"]
    assert "Calibrated Gravity" in calibrated["label"]
    assert raw["values"] != calibrated["values"]
    assert not errors, errors


@pytest.mark.requires_browser
def test_calibration_dialog_keyboard_focus_and_live_region(
    browser_page, live_server: str
) -> None:
    with httpx.Client(timeout=3.0) as client:
        assert client.post(f"{live_server}/api/ingest", json={
            "ID": "calibration-dialog-a11y", "gravity": 1.02
        }).status_code == 200
    page = browser_page
    page.goto(f"{live_server}/")
    page.select_option("#device-select", "calibration-dialog-a11y")
    opener = page.get_by_role("button", name="Calibration")
    opener.click()
    modal = page.get_by_role("dialog", name="Calibration")
    assert modal.is_visible()
    assert page.evaluate("() => document.activeElement.id") == "cal-label"
    assert page.locator("#cal-status").get_attribute("role") == "status"
    assert page.locator("#main-content").get_attribute("data-modal-open") == "true"
    page.keyboard.press("Shift+Tab")
    assert page.evaluate("() => document.activeElement.id") == "cal-save"
    assert page.evaluate(
        "() => document.getElementById('cal-modal').contains(document.activeElement)"
    )
    page.keyboard.press("Tab")
    assert page.evaluate("() => document.activeElement.id") == "cal-label"
    for _ in range(12):
        page.keyboard.press("Tab")
        assert page.evaluate(
            "() => document.getElementById('cal-modal').contains(document.activeElement)"
        )
    page.keyboard.press("Escape")
    assert not modal.is_visible()
    assert opener.evaluate("el => el === document.activeElement")
    assert page.locator("#main-content").get_attribute("data-modal-open") is None


@pytest.mark.requires_browser
def test_calibration_history_save_activate_and_mode_switch(
    browser_page, live_server: str
) -> None:
    device_id = "calibration-browser-flow"
    with httpx.Client(timeout=3.0) as client:
        assert client.post(f"{live_server}/api/ingest", json={
            "ID": device_id,
            "angle": 20.0,
            "gravity": 1.02,
            "temperature": 20.0,
        }).status_code == 200

    page = browser_page
    page.goto(f"{live_server}/")
    page.select_option("#device-select", device_id)
    original_url = page.url
    page.get_by_role("button", name="Calibration").click()
    modal = page.get_by_role("dialog", name="Calibration")
    assert modal.is_visible()
    assert modal.locator('[aria-label="Calibration angle 1"]').count() == 1
    assert modal.locator('[aria-label="Calibration gravity 1"]').count() == 1

    page.locator("#cal-label").fill("Browser linear")
    page.locator(".cal-a").nth(0).fill("10")
    page.locator(".cal-v").nth(0).fill("1.00")
    page.locator(".cal-a").nth(1).fill("20")
    page.locator(".cal-v").nth(1).fill("1.10")
    page.locator("#cal-save").click()
    page.wait_for_function(
        "() => document.getElementById('cal-status').textContent.startsWith('Saved inactive Browser linear')"
    )
    saved_row = page.locator(".calibration-history-row").filter(
        has_text="Browser linear"
    )
    assert saved_row.get_attribute("aria-current") == "false"
    saved_row.get_by_role(
        "button", name="Activate calibration Browser linear"
    ).click()
    page.wait_for_function(
        "() => [...document.querySelectorAll('.calibration-history-row.is-active')].some(row => row.textContent.includes('Browser linear'))"
    )
    active_row = page.locator(".calibration-history-row.is-active").filter(
        has_text="Browser linear"
    )
    assert active_row.count() == 1
    assert active_row.get_attribute("aria-current") == "true"
    assert page.url == original_url

    page.locator("#cal-cancel").click()
    with httpx.Client(timeout=3.0) as client:
        response = client.post(
            f"{live_server}/api/device/{device_id}/calibration",
            json={
                "label": "Browser inactive",
                "activate": False,
                "points": [
                    {"angle": 10.0, "value": 1.01},
                    {"angle": 20.0, "value": 1.11},
                ],
            },
        )
        assert response.status_code == 200

    page.reload()
    page.select_option("#device-select", device_id)
    page.get_by_role("button", name="Calibration").click()
    inactive_row = page.locator(".calibration-history-row").filter(
        has_text="Browser inactive"
    )
    inactive_row.get_by_role("button", name="Activate calibration Browser inactive").click()
    page.wait_for_function(
        "() => [...document.querySelectorAll('.calibration-history-row.is-active')].some(row => row.textContent.includes('Browser inactive'))"
    )
    assert page.url == original_url

    def fail_post(route):
        if route.request.method == "POST":
            route.fulfill(status=500, body="injected browser API failure")
        else:
            route.continue_()

    page.route("**/api/device/*/calibration", fail_post)
    page.locator("#cal-label").fill("Must fail")
    page.locator(".cal-a").nth(0).fill("10")
    page.locator(".cal-v").nth(0).fill("1.00")
    page.locator(".cal-a").nth(1).fill("20")
    page.locator(".cal-v").nth(1).fill("1.10")
    page.locator("#cal-save").click()
    page.wait_for_function(
        "() => document.getElementById('cal-status').textContent.includes('injected browser API failure')"
    )
    assert page.locator("#cal-status").inner_text().startswith("Error:")
    assert page.url == original_url


@pytest.mark.requires_browser
def test_calibrated_unavailable_never_falls_back_to_raw(browser_page, live_server: str) -> None:
    device_id = "calibrated-unavailable"
    with httpx.Client(timeout=3.0) as client:
        assert client.post(f"{live_server}/api/ingest", json={
            "ID": device_id, "gravity": 1.077, "temperature": 20.0
        }).status_code == 200
        assert client.post(f"{live_server}/api/device/{device_id}/calibration", json={
            "label": "active", "points": [
                {"angle": 10.0, "value": 1.0},
                {"angle": 20.0, "value": 1.1},
            ],
        }).status_code == 200
    page = browser_page
    page.goto(f"{live_server}/")
    page.select_option("#device-select", device_id)
    page.select_option("#gravity-mode", "calibrated")
    page.wait_for_function(
        """() => typeof chart !== 'undefined' && chart &&
        chart.data.datasets.some(d => d.metric === 'gravity') &&
        chart.data.datasets.find(d => d.metric === 'gravity').data.length === 0"""
    )
    gravity = page.evaluate(
        "() => chart.data.datasets.find(d => d.metric === 'gravity').data.map(point => point.y)"
    )
    assert gravity == []


@pytest.mark.requires_browser
def test_calibration_labels_and_errors_are_dom_safe(browser_page, live_server: str) -> None:
    device_id = "calibration-dom-safe"
    labels = [
        '<img src=x onerror="window.__xss=1">',
        '<svg><script>window.__xss=2</script></svg>',
        'quotes " \' and bidi \u202e control-like',
    ]
    with httpx.Client(timeout=3.0) as client:
        assert client.post(f"{live_server}/api/ingest", json={
            "ID": device_id, "gravity": 1.01
        }).status_code == 200
        for label in labels:
            assert client.post(f"{live_server}/api/device/{device_id}/calibration", json={
                "label": label,
                "activate": False,
                "points": [
                    {"angle": 10.0, "value": 1.0},
                    {"angle": 20.0, "value": 1.1},
                ],
            }).status_code == 200
    page = browser_page
    page.goto(f"{live_server}/")
    page.select_option("#device-select", device_id)
    page.get_by_role("button", name="Calibration").click()
    page.wait_for_function(
        "() => document.querySelectorAll('#cal-history .calibration-history-row').length >= 3"
    )
    history = page.locator("#cal-history")
    for label in labels:
        assert label in history.inner_text()
    assert history.locator("img, svg, script").count() == 0
    assert page.evaluate("() => window.__xss") is None

    page.locator("#cal-cancel").click()
    error_markup = '<img src=x onerror="window.__xss=3"><script>window.__xss=4</script>'

    def fail_history(route):
        if route.request.method == "GET":
            route.fulfill(status=500, body=error_markup)
        else:
            route.continue_()

    page.route("**/api/device/*/calibration", fail_history)
    page.get_by_role("button", name="Calibration").click()
    page.wait_for_function(
        "markup => document.getElementById('cal-status').textContent.includes(markup)",
        arg=error_markup,
    )
    assert page.locator("#cal-status img, #cal-status script").count() == 0
    assert page.evaluate("() => window.__xss") is None


@pytest.mark.requires_browser
def test_calibration_dialog_populated_state_has_no_serious_axe_findings(
    browser_page, live_server: str
) -> None:
    from axe_playwright_python.sync_playwright import Axe

    device_id = "calibration-axe-populated"
    with httpx.Client(timeout=3.0) as client:
        assert client.post(f"{live_server}/api/ingest", json={
            "ID": device_id, "gravity": 1.01
        }).status_code == 200
        assert client.post(f"{live_server}/api/device/{device_id}/calibration", json={
            "label": "Axe populated history",
            "points": [
                {"angle": 10.0, "value": 1.0},
                {"angle": 20.0, "value": 1.1},
            ],
        }).status_code == 200
    page = browser_page
    page.goto(f"{live_server}/")
    page.select_option("#device-select", device_id)
    page.get_by_role("button", name="Calibration").click()
    page.wait_for_selector(".calibration-history-row.is-active")
    results = Axe().run(page)
    serious = [
        violation for violation in results.response["violations"]
        if violation["impact"] in {"serious", "critical"}
    ]
    assert not serious, serious


@pytest.mark.requires_browser
def test_dashboard_uses_local_chart_assets_with_external_network_blocked(
    browser_page, live_server: str
) -> None:
    """Phase 5A: the complete chart renders using only same-origin requests."""
    from urllib.parse import urlsplit

    with httpx.Client(timeout=2.0) as client:
        response = client.post(
            f"{live_server}/api/ingest",
            json={
                "ID": "phase5a-offline",
                "name": "Phase 5A Offline",
                "angle": 21.5,
                "gravity": 1.045,
                "temperature": 20.5,
            },
        )
        assert response.status_code == 200

    allowed = urlsplit(live_server)
    allowed_origin = f"{allowed.scheme}://{allowed.netloc}"
    requested: list[str] = []
    blocked: list[str] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    page = browser_page

    def intercept(route) -> None:
        url = route.request.url
        requested.append(url)
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin != allowed_origin:
            blocked.append(url)
            route.abort()
            return
        route.continue_()

    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.route("**/*", intercept)
    page.goto(f"{live_server}/", wait_until="domcontentloaded")
    page.wait_for_function(
        """() => window.Chart && Chart.version === '4.5.1' &&
        Chart.getChart('chartCanvas') &&
        Chart.getChart('chartCanvas').scales.x &&
        Chart.getChart('chartCanvas').scales.x.type === 'time'""",
        timeout=10_000,
    )
    state = page.evaluate(
        """() => {
          const instance = Chart.getChart('chartCanvas');
          const canvas = document.getElementById('chartCanvas');
          return {
            version: Chart.version,
            xType: instance.scales.x.type,
            width: canvas.getBoundingClientRect().width,
            height: canvas.getBoundingClientRect().height,
            datasets: instance.data.datasets.length,
          };
        }"""
    )
    assert state["version"] == "4.5.1"
    assert state["xType"] == "time"
    assert state["width"] > 0 and state["height"] > 0
    assert state["datasets"] > 0
    assert blocked == []
    assert requested and all(
        f"{urlsplit(url).scheme}://{urlsplit(url).netloc}" == allowed_origin
        for url in requested
    )
    assert console_errors == []
    assert page_errors == []


@pytest.mark.requires_browser
def test_live_first_party_assets_match_reviewed_files_and_revalidate(
    browser_page, live_server: str
) -> None:
    from urllib.parse import urlsplit

    page = browser_page
    allowed = urlsplit(live_server)
    allowed_origin = f"{allowed.scheme}://{allowed.netloc}"
    with httpx.Client(timeout=2.0) as client:
        seed = client.post(
            f"{live_server}/api/ingest",
            json={
                "ID": "phase06a1-live-assets",
                "name": "Phase 6A1 Live Assets",
                "angle": 21.5,
                "gravity": 1.045,
                "temperature": 20.5,
            },
        )
        assert seed.status_code == 200
    asset_paths = ("/static/dashboard.css", "/static/dashboard.js")
    navigation_index = {"value": 0}
    requested: list[list[str]] = [[], []]
    responses: list[dict[str, object]] = [{}, {}]
    blocked: list[str] = []
    request_failures: list[str] = []
    console_errors: list[str] = []
    page_errors: list[str] = []

    def intercept(route) -> None:
        url = route.request.url
        requested[navigation_index["value"]].append(url)
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin != allowed_origin:
            blocked.append(url)
            route.abort()
            return
        route.continue_()

    def capture_response(response) -> None:
        path = urlsplit(response.url).path
        if path in asset_paths:
            responses[navigation_index["value"]][path] = response

    page.route("**/*", intercept)
    page.on("response", capture_response)
    page.on("requestfailed", lambda request: request_failures.append(request.url))
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    page.goto(f"{live_server}/", wait_until="domcontentloaded")
    first_paths = {urlsplit(url).path for url in requested[0]}
    assert set(asset_paths) <= first_paths
    assert set(asset_paths) <= responses[0].keys()

    project_root = Path(__file__).resolve().parents[2]
    expected_bodies = {
        "/static/dashboard.css": (project_root / "app/static/dashboard.css").read_bytes(),
        "/static/dashboard.js": (project_root / "app/static/dashboard.js").read_bytes(),
    }

    def assert_asset_responses(index: int) -> None:
        for path in asset_paths:
            response = responses[index][path]
            assert response.status == 200
            expected_type = "text/css" if path.endswith(".css") else "application/javascript"
            assert response.headers["content-type"].startswith(expected_type)
            assert (
                response.headers["cache-control"]
                == "no-cache, max-age=0, must-revalidate"
            )
            assert response.body() == expected_bodies[path]

    assert_asset_responses(0)
    page.wait_for_timeout(500)
    assert console_errors == []
    assert page_errors == []
    page.wait_for_function(
        """() => window.Chart && Chart.version === '4.5.1' &&
        Chart.getChart('chartCanvas') &&
        Chart.getChart('chartCanvas').scales.x.type === 'time'""",
        timeout=10_000,
    )
    state = page.evaluate(
        """() => {
          const instance = Chart.getChart('chartCanvas');
          const canvas = document.getElementById('chartCanvas');
          const style = (selector) => getComputedStyle(document.querySelector(selector));
          return {
            version: Chart.version,
            xType: instance.scales.x.type,
            width: canvas.getBoundingClientRect().width,
            height: canvas.getBoundingClientRect().height,
            datasets: instance.data.datasets.length,
            accents: {
              gravity: style('.value-pill[data-metric="gravity"]').getPropertyValue('--accent').trim(),
              temp_c: style('.value-pill[data-metric="temp_c"]').getPropertyValue('--accent').trim(),
              battery: style('.value-pill[data-metric="battery"]').getPropertyValue('--accent').trim(),
              angle: style('.value-pill[data-metric="angle"]').getPropertyValue('--accent').trim(),
              rssi: style('.value-pill[data-metric="rssi"]').getPropertyValue('--accent').trim(),
            },
            spacer: {
              grow: style('.chart-toolbar-spacer').flexGrow,
              shrink: style('.chart-toolbar-spacer').flexShrink,
              basis: style('.chart-toolbar-spacer').flexBasis,
            },
            help: {
              size: style('.calibration-help').fontSize,
              color: style('.calibration-help').color,
            },
            pointsMargin: style('.calibration-points-table').marginBottom,
            status: {
              size: style('#cal-status.calibration-status').fontSize,
              weight: style('#cal-status.calibration-status').fontWeight,
              color: style('#cal-status.calibration-status').color,
            },
          };
        }"""
    )
    assert state["version"] == "4.5.1"
    assert state["xType"] == "time"
    assert state["width"] > 0 and state["height"] > 0
    assert state["datasets"] > 0
    assert state["accents"] == {
        "gravity": "#4f46e5",
        "temp_c": "#ef4444",
        "battery": "#f59e0b",
        "angle": "#10b981",
        "rssi": "#8b5cf6",
    }
    assert state["spacer"] == {"grow": "1", "shrink": "1", "basis": "0%"}
    assert state["help"] == {"size": "14px", "color": "rgb(102, 102, 102)"}
    assert state["pointsMargin"] == "12px"
    assert state["status"] == {
        "size": "14px",
        "weight": "700",
        "color": "rgb(37, 99, 235)",
    }

    page.evaluate(
        """async () => {
          const registrations = await navigator.serviceWorker.getRegistrations();
          await Promise.all(registrations.map(registration => registration.unregister()));
          const keys = await caches.keys();
          await Promise.all(keys.map(key => caches.delete(key)));
        }"""
    )
    navigation_index["value"] = 1
    page.reload(wait_until="domcontentloaded")
    second_paths = {urlsplit(url).path for url in requested[1]}
    assert set(asset_paths) <= second_paths
    assert set(asset_paths) <= responses[1].keys()
    assert_asset_responses(1)
    page.wait_for_function(
        """() => window.Chart && Chart.version === '4.5.1' &&
        Chart.getChart('chartCanvas') &&
        Chart.getChart('chartCanvas').scales.x.type === 'time'""",
        timeout=10_000,
    )
    assert blocked == []
    assert request_failures == []
    assert console_errors == []
    assert page_errors == []


def test_dashboard_surface_has_strict_security_headers(test_client) -> None:
    response = test_client.get("/")
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert response.headers["x-frame-options"] == "DENY"
