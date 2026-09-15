# V3 Research Subagent A: iSpindel Dashboard Investigation Report

## 1. Exact pytest/Playwright Test Harness
*   **Observed Evidence:** The project currently has zero tests (no `tests/` directory). The `requirements.txt` and `Dockerfile` lack testing libraries. No CI/CD or local test scripts exist to validate HTML/JS behaviors.
*   **Root Cause:** Initial implementation prioritized rapid delivery of the single-file FastAPI/Vanilla JS monolith over testability.
*   **Exact Proposed Contract:**
    *   Initialize a Pytest suite utilizing `pytest-playwright` and `axe-playwright-python`.
    *   Create a dependency-injected SQLite in-memory database fixture (`sqlite3.connect(":memory:")`) to ensure strict per-test isolation for the FastAPI backend.
    *   Install browser binaries locally via `playwright install chromium` as a pre-test step.
    *   Integrate axe-core for automated accessibility constraint checks on every E2E page render.
*   **Exact Files/Functions:**
    *   New: `tests/conftest.py` (fixtures for FastAPI `TestClient`, ephemeral SQLite DB, and Playwright `page`).
    *   New: `tests/test_api.py`, `tests/test_browser_ui.py`.
    *   Modify: `requirements-test.txt` (or update `requirements.txt` / `pyproject.toml`).
*   **Test Names & RED/GREEN:**
    *   *Test:* `test_dashboard_axe_accessibility`
    *   *RED Command:* `pytest tests/test_browser_ui.py -k test_dashboard_axe_accessibility` (Expect: Fails immediately due to current missing `lang` and ARIA violations).
    *   *GREEN Command:* Re-run after DOM patches. Expect: Passes.
*   **Rollout Proof:** `pytest tests/` executes locally and in CI with 100% pass rate.
*   **Rollback/STOP Gate:** Abort if fixture isolation fails (state bleeds between tests) or if Playwright fails to launch the browser environment.
*   **Dependencies:** `pytest`, `pytest-asyncio`, `pytest-playwright`, `axe-playwright-python`, `httpx`.
*   **Authoritative URLs:** Playwright Python (https://playwright.dev/python/), FastAPI Testing (https://fastapi.tiangolo.com/tutorial/testing/).
*   **Trade-offs & Defaults:** *Default:* Use Chromium-only for E2E tests to save CI time and disk space, unless specific WebKit/Firefox bugs are reported.

## 2. Graph Behavior Contracts
*   **Observed Evidence:** `app/main.py` lines 876-956. The X-axis `min` is frozen during active polling (only `max` is patched). Multi-axis display applies `reverse: idx % 2 === 1` causing visual collision. `refreshAll()` causes a race condition by destroying the chart before new data arrives. CSS vs JS resize implementation conflicts.
*   **Root Cause:** Suboptimal Chart.js runtime updates. Polling logic appends data but fails to slide the temporal window. `buildScales` initializes axes inconsistently with dataset visibility.
*   **Exact Proposed Contract:**
    *   **X-Range:** Assign `chart.options.scales.x.min = xRange.min` and `.max = xRange.max` explicitly inside the polling loop to slide the window.
    *   **Multi-axis:** Remove `reverse: idx % 2 === 1`. Ensure all axes stay `position: 'left'`. Use `display: vis.has(m.id)` to strictly hide/show axes.
    *   **Refresh:** Ensure `refreshAll()` awaits `loadChart()` before pulse execution. Track `let isFetching = false` to prevent overlapping network calls.
    *   **Deltas:** Calculate adjacent-sample deltas using `samples[samples.length - 1]` and `samples[samples.length - 2]`, discarding the flawed cross-poll `lastByMetric` state.
    *   **Resize:** Implement full-width pointer events using `setPointerCapture` and a `ResizeObserver`, storing bounds in `localStorage`.
*   **Exact Files/Functions:** `app/main.py` (`updateChartLive`, `buildScales`, `refreshAll`, `startChartPolling`, `updateValuePills`, `initChartResize`).
*   **Test Names & RED/GREEN:**
    *   *Test:* `test_chart_sliding_x_axis` (inject simulated time flow).
    *   *RED Command:* `pytest tests/test_browser_ui.py -k test_chart_sliding_x_axis` (Expect: X-axis `min` remains static).
    *   *GREEN Command:* Re-run after applying x-range assignment.
*   **Rollout Proof:** Visual validation: left-side axes order deterministically, X-axis slides smoothly, and manual refresh does not flash a blank canvas.
*   **Rollback/STOP Gate:** Revert `app/main.py` if Chart.js throws uncaught type errors in the browser console.
*   **Dependencies:** Chart.js 4.4 API.
*   **Authoritative URLs:** Chart.js Axes (https://www.chartjs.org/docs/latest/axes/), MDN Pointer Events (https://developer.mozilla.org/en-US/docs/Web/API/Pointer_events).
*   **Trade-offs & Defaults:** *Default:* Use JS-driven Pointer Events for resizing over native CSS resize to allow a custom full-width grab handle for better UX.

## 3. DOM/XSS Protection
*   **Observed Evidence:** `app/main.py` line 1102 interpolates unescaped device names via string templating. HTML attributes use inline handlers (e.g., `onclick="openEditModal(...)"`). The `PATCH /api/device/{device_id}` endpoint does not limit request sizes or return 404 for missing records.
*   **Root Cause:** Rapid monolithic prototyping relying on vanilla JS `innerHTML` without DOM sanitization.
*   **Exact Proposed Contract:**
    *   **XSS:** Replace all `innerHTML` injections of untrusted user input (Device Name, SSID) with strict `textContent` assignments.
    *   **Handlers:** Strip inline `onclick` attributes. Attach delegated event listeners using data attributes (`data-device-id`).
    *   **API:** Implement Pydantic validation for `PATCH /api/device/{id}`. Explicitly return HTTP 404 if the `device_id` does not exist in SQLite. Add a FastAPI middleware or request boundary to reject payloads > 64KB.
*   **Exact Files/Functions:** `app/main.py` (HTML `<script>` block, `update_device` FastAPI route).
*   **Test Names & RED/GREEN:**
    *   *Test:* `test_xss_device_name_escaped`
    *   *RED Command:* `pytest tests/test_api.py -k test_xss_device_name_escaped` (Expect: payload renders as executable HTML).
    *   *GREEN Command:* Re-run after `textContent` refactor.
*   **Rollout Proof:** Input `<script>alert(1)</script>` as a device name; verify it displays literally in the DOM without execution.
*   **Rollback/STOP Gate:** Legitimate inputs are aggressively over-escaped (e.g., displaying `&lt;` instead of `<`).
*   **Dependencies:** FastAPI/Pydantic, DOM standard library.
*   **Authoritative URLs:** OWASP XSS (https://owasp.org/www-community/attacks/xss/), MDN textContent (https://developer.mozilla.org/en-US/docs/Web/API/Node/textContent).
*   **Trade-offs & Defaults:** *Default:* Rely on native DOM `textContent` rather than pulling in a heavy dependency like DOMPurify, as the app does not legitimately require rendering user HTML.

## 4. Complete Accessibility/Mobile Contract
*   **Observed Evidence:** Missing `<html lang="en">`. Select dropdowns lack `<label>` associations. Modals do not trap focus or respond to the `Escape` key. Resize handle lacks correct ARIA slider semantics. No screen-reader alternative for the canvas.
*   **Root Cause:** A11y constraints were omitted during the initial UI draft.
*   **Exact Proposed Contract:**
    *   **Semantics:** Apply `lang="en"` and explicit `<label for="id">` to all controls.
    *   **Modal:** Implement a keyboard focus trap. Bind the `Escape` key to `closeModal()`. Restore `document.activeElement` to the trigger button upon close.
    *   **Resize:** Apply `role="separator"`, `aria-orientation="horizontal"`, `aria-valuemin`, `aria-valuemax`, and update `aria-valuenow` dynamically.
    *   **Data Alternative:** Inject a visually hidden `<table>` (via `.sr-only` CSS) containing the raw chart data for screen readers.
    *   **Mobile:** Apply `overflow-x: auto` to tables to prevent viewport blowing out.
*   **Exact Files/Functions:** `app/main.py` (HTML structure, CSS, modal open/close functions, resize observer).
*   **Test Names & RED/GREEN:**
    *   *Test:* `test_modal_keyboard_trap_and_escape`
    *   *RED Command:* `pytest tests/test_browser_ui.py -k test_modal_keyboard_trap_and_escape` (Expect: Focus escapes the modal; Escape key does nothing).
    *   *GREEN Command:* Re-run after implementing focus trap logic.
*   **Rollout Proof:** Axe-core reports zero violations. VoiceOver/NVDA successfully reads the hidden chart data table. Tab navigation stays contained within the open modal.
*   **Rollback/STOP Gate:** Severe layout regressions on iOS Safari/Chrome mobile viewports.
*   **Dependencies:** axe-core.
*   **Authoritative URLs:** W3C APG Dialog (https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/), MDN WAI-ARIA (https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA).
*   **Trade-offs & Defaults:** *Default:* Use a visually hidden table over `aria-label` descriptions on the canvas for complex time-series data, as it provides far superior navigation for screen reader users.

## 5. System Health Integration
*   **Observed Evidence:** `app/main.py:321-322` hardcodes absolute host paths (`<deploy-user-home>/.zeroclaw/...`) which silently fail inside the Docker container.
*   **Root Cause:** Container boundary isolation was ignored; FastAPI attempts to read the host filesystem directly.
*   **Exact Proposed Contract:**
    *   **Mounts:** Add a read-only bind mount in `docker-compose.yml`: `- ~/.zeroclaw/workspace/memory:/zeroclaw_memory:ro`.
    *   **Config:** Use environment variables (e.g., `ZEROCLAW_MEMORY_PATH=/zeroclaw_memory`) inside FastAPI instead of hardcoded strings.
    *   **Logic:** Parse the heartbeat file and apply a strict TTL (e.g., `stale` if timestamp > 5 minutes old).
    *   **Taxonomy:** Standardize states: `ok`, `warning`, `critical`, `stale`, `missing`, `parse_error`.
    *   **Ownership:** FastAPI exposes the `/api/system-health` endpoint *only*. Zeroclaw retains exclusive ownership of alerting rules.
*   **Exact Files/Functions:** `docker-compose.yml` (volumes), `app/main.py` (`system_health()`).
*   **Test Names & RED/GREEN:**
    *   *Test:* `test_system_health_stale_heartbeat`
    *   *RED Command:* `pytest tests/test_api.py -k test_system_health_stale_heartbeat` (Expect: API returns 500 or ignores stale logic).
    *   *GREEN Command:* Re-run after TTL parsing is applied via mocked env vars.
*   **Rollout Proof:** Deploy via Docker Compose. The dashboard UI correctly reflects the battery state and flags a stale heartbeat if the file hasn't been touched in 5 minutes.
*   **Rollback/STOP Gate:** Docker container fails to start due to volume mount permission denied errors.
*   **Dependencies:** Standard Python library (`json`, `os`, `pathlib`).
*   **Authoritative URLs:** Docker Compose Volumes (https://docs.docker.com/compose/compose-file/05-services/#volumes).
*   **Trade-offs & Defaults:** *Default:* Keep the integration read-only via file mounts rather than building a heavy HTTP/WebSocket bridge to Zeroclaw, maintaining loose coupling and respecting the architecture mandate.
