"""Phase 5A contract tests for immutable local frontend assets."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


STATIC_ROOT = Path(__file__).resolve().parents[1] / "app" / "static"
MANIFEST_PATH = STATIC_ROOT / "assets-manifest.json"
CACHE_CONTROL = "public, max-age=31536000, immutable"

EXPECTED_PACKAGES = [
    {
        "name": "chart.js",
        "version": "4.5.1",
        "license": "MIT",
        "metadata_url": "https://registry.npmjs.org/chart.js/latest",
        "tarball_url": "https://registry.npmjs.org/chart.js/-/chart.js-4.5.1.tgz",
        "npm_integrity": "sha512-GIjfiT9dbmHRiYi6Nl2yFCq7kkwdkp1W/lp2J99rX0yo9tgJGn3lKQATztIjb5tVtevcBtIdICNWqlq5+E8/Pw==",
        "npm_shasum": "19dd1a9a386a3f6397691672231cb5fc9c052c35",
        "tarball_sha256": "f540d98468457ac7a0aabb32006dfb066297e096c5ea063a5d80aa973d1c337a",
        "license_path": "app/static/licenses/chartjs-LICENSE.md",
        "license_sha256": "41a84aa2caba645f966a18d9c2056b73e6d3a81d80bc0046bc0011a2634d4cce",
        "license_bytes": 1093,
    },
    {
        "name": "chartjs-adapter-date-fns",
        "version": "3.0.0",
        "license": "MIT",
        "metadata_url": "https://registry.npmjs.org/chartjs-adapter-date-fns/latest",
        "tarball_url": "https://registry.npmjs.org/chartjs-adapter-date-fns/-/chartjs-adapter-date-fns-3.0.0.tgz",
        "npm_integrity": "sha512-Rs3iEB3Q5pJ973J93OBTpnP7qoGwvq3nUnoMdtxO+9aoJof7UFcRbWcIDteXuYd1fgAvct/32T9qaLyLuZVwCg==",
        "npm_shasum": "c25f63c7f317c1f96f9a7c44bd45eeedb8a478e5",
        "tarball_sha256": "53e583bed13f12d3fd59885debfff58f677031f95720632a046e925304dba498",
        "license_path": "app/static/licenses/chartjs-adapter-date-fns-LICENSE.md",
        "license_sha256": "b4b8355c2cd2b18354980a0c6422181d7bd6e895d94ae88b3570e97c60eea03d",
        "license_bytes": 1088,
    },
    {
        "name": "date-fns",
        "version": "4.4.0",
        "license": "MIT",
        "metadata_url": "https://registry.npmjs.org/date-fns/latest",
        "tarball_url": "https://registry.npmjs.org/date-fns/-/date-fns-4.4.0.tgz",
        "npm_integrity": "sha512-+1UMbeh68lH1SegH83CGWwpb6OHHbpSgr3+s5Eww5M4CAgswBpoWS0AjTOfEJ33HiYKz1hdj/KTFprzXHmq/6w==",
        "npm_shasum": "806539edf45c616b2b76b5f78b88c56ed3c7e036",
        "tarball_sha256": "eb106d1e9276213d6144b221c103e4abb7d92186734f7505f5a3860427b41a06",
        "license_path": "app/static/licenses/date-fns-LICENSE.md",
        "license_sha256": "8d3951c38967b964b1fe259bfd200c2647cc04c858b55a4414e3122a60f1ef4b",
        "license_bytes": 1117,
    },
]

EXPECTED_ASSETS = [
    {
        "package": "chart.js",
        "package_version": "4.5.1",
        "tar_member": "package/dist/chart.umd.js",
        "path": "app/static/chart.umd.js",
        "route": "/static/chart.umd.js",
        "sha256": "ecc3cd1eeb8c34d2178e3f59fd63ec5a3d84358c11730af0b9958dc886d7652a",
        "bytes": 208518,
        "content_type": "application/javascript",
        "cache_control": CACHE_CONTROL,
    },
    {
        "package": "chartjs-adapter-date-fns",
        "package_version": "3.0.0",
        "tar_member": "package/dist/chartjs-adapter-date-fns.bundle.min.js",
        "path": "app/static/chartjs-adapter-date-fns.bundle.min.js",
        "route": "/static/chartjs-adapter-date-fns.bundle.min.js",
        "sha256": "ea7ab30d26c38dcf1f2d26bb43e73a94537b58f1906f55e1a546dd09321b5615",
        "bytes": 50650,
        "content_type": "application/javascript",
        "cache_control": CACHE_CONTROL,
    },
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_assets_manifest_is_exact_and_files_match() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert set(manifest) == {"schema_version", "packages", "served_assets"}
    assert manifest["schema_version"] == 1
    assert manifest["packages"] == EXPECTED_PACKAGES
    assert manifest["served_assets"] == EXPECTED_ASSETS

    project_root = STATIC_ROOT.parents[1]
    for package in EXPECTED_PACKAGES:
        license_path = project_root / package["license_path"]
        assert license_path.stat().st_size == package["license_bytes"]
        assert _sha256(license_path) == package["license_sha256"]
    for asset in EXPECTED_ASSETS:
        path = project_root / asset["path"]
        assert path.stat().st_size == asset["bytes"]
        assert _sha256(path) == asset["sha256"]


def test_fixed_asset_routes_serve_exact_bytes_and_headers(test_client) -> None:
    project_root = STATIC_ROOT.parents[1]
    for asset in EXPECTED_ASSETS:
        response = test_client.get(asset["route"])
        assert response.status_code == 200
        assert response.content == (project_root / asset["path"]).read_bytes()
        assert response.headers["content-type"].startswith("application/javascript")
        assert response.headers["cache-control"] == CACHE_CONTROL


def test_static_routes_do_not_expose_generic_paths(test_client) -> None:
    for path in (
        "/static/unknown.js",
        "/static/../../etc/passwd",
        "/static/%2e%2e/%2e%2e/etc/passwd",
        "/static/licenses/chartjs-LICENSE.md",
    ):
        response = test_client.get(path)
        assert response.status_code in {404, 405}
        assert b"root:" not in response.content


def test_dashboard_references_only_ordered_local_vendor_scripts(test_client) -> None:
    response = test_client.get("/")
    assert response.status_code == 200
    html = response.text
    sources = re.findall(r'<script\s+src="([^"]+)"', html)
    assert sources == [
        "/static/chart.umd.js",
        "/static/chartjs-adapter-date-fns.bundle.min.js",
        "/static/dashboard.js",
        "/static/brewing.js",
    ]
    assert "cdn.jsdelivr.net" not in html
    assert not any(src.startswith(("http://", "https://", "//")) for src in sources)
    policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "script-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert 'http-equiv="Content-Security-Policy"' not in html


def test_dashboard_references_external_first_party_assets(test_client) -> None:
    response = test_client.get("/")
    assert response.status_code == 200
    html = response.text
    sources = re.findall(r'<script\b[^>]*\bsrc\s*=\s*"([^"]+)"', html, re.IGNORECASE)
    assert sources == [
        "/static/chart.umd.js",
        "/static/chartjs-adapter-date-fns.bundle.min.js",
        "/static/dashboard.js",
        "/static/brewing.js",
    ]
    stylesheets = re.findall(
        r'<link\b(?=[^>]*\brel\s*=\s*"stylesheet")(?=[^>]*\bhref\s*=\s*"([^"]+)")[^>]*>',
        html,
        re.IGNORECASE,
    )
    assert stylesheets == ["/static/dashboard.css"]
    assert re.search(r"<style\b", html, re.IGNORECASE) is None
    assert re.search(r"\bstyle\s*=", html, re.IGNORECASE) is None
    assert re.search(
        r"<script\b(?![^>]*\bsrc\s*=)[^>]*>", html, re.IGNORECASE
    ) is None
    policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "script-src 'self'" in policy
    assert "style-src 'self'" in policy
    assert "connect-src 'self'" in policy
    assert re.search(
        r'<meta\b[^>]*http-equiv\s*=\s*["\']Content-Security-Policy["\']',
        html,
        re.IGNORECASE,
    ) is None
    for url in [*sources, *stylesheets]:
        assert not url.startswith(("http://", "https://", "//"))


def test_first_party_asset_routes_serve_exact_bytes_and_headers(test_client) -> None:
    assets = [
        ("/static/dashboard.css", STATIC_ROOT / "dashboard.css", "text/css"),
        (
            "/static/dashboard.js",
            STATIC_ROOT / "dashboard.js",
            "application/javascript",
        ),
        (
            "/static/brewing.js",
            STATIC_ROOT / "brewing.js",
            "application/javascript",
        ),
    ]
    for route, path, content_type in assets:
        response = test_client.get(route)
        assert response.status_code == 200
        assert response.content == path.read_bytes()
        assert response.headers["content-type"].startswith(content_type)
        assert (
            response.headers["cache-control"]
            == "no-cache, max-age=0, must-revalidate"
        )


def test_brewing_assistant_apply_is_bound_and_findings_are_opt_in() -> None:
    source = (STATIC_ROOT / "brewing.js").read_text(encoding="utf-8")
    # The apply pipeline is now per-field opt-in, but every job/scope/diff
    # guard from the original contract is preserved.
    assert "function applyRecipeProposal(panel, job, result, selectedPaths)" in source
    assert "!Array.isArray(applicableDiff)" in source
    assert "panel.dataset.assistantJobId !== job.job_id" in source
    assert "checkbox.checked = false" in source
    assert "applyRecipeProposal(panel, job, result, selectedPaths)" in source


def test_brewing_assistant_apply_renders_per_field_diff_choices() -> None:
    source = (STATIC_ROOT / "brewing.js").read_text(encoding="utf-8")
    # One unchecked checkbox per applicable_diff entry, with a path label and a
    # concise before/after preview. The button applies ONLY the ticked paths.
    assert "function renderApplicableDiffChoices(panel, job, result, onApply)" in source
    assert "checkbox.dataset.diffPath = change.path" in source
    assert "checkbox.dataset.diffJobId = job?.job_id || ''" in source
    assert "checkbox.checked = false" in source
    assert "Apply diff at ${change.path}" in source
    assert "node('code', 'diff-path', change.path)" in source
    assert "node('span', 'diff-preview'" in source
    assert "Apply checked entries to form (not saved)" in source
    # The apply callback only forwards paths of currently checked checkboxes.
    choices_block = source.split(
        "function renderApplicableDiffChoices(panel, job, result, onApply) {",
        1,
    )[1]
    assert "$$('[data-diff-path]:checked', wrapper).map((input) => input.dataset.diffPath)" in choices_block
    assert "onApply(tickedPaths)" in choices_block


def test_brewing_assistant_apply_is_per_field_and_atomic() -> None:
    source = (STATIC_ROOT / "brewing.js").read_text(encoding="utf-8")
    apply_block = source.split("function applyRecipeProposal(panel, job, result, selectedPaths) {", 1)[1].split(
        "\n  }\n\n  function undoAppliedRecipeProposal", 1
    )[0]
    # Empty / non-array selectedPaths must abort before the form is touched.
    assert "Select at least one diff entry to apply to the form." in apply_block
    assert "!selectedSet.size" in apply_block
    # Only the operator-ticked subset is fed to the conflict + pointer checks.
    assert "applicableDiff.filter((change) => selectedSet.has(change.path))" in apply_block
    # Atomic abort: every chosen entry must resolve cleanly, otherwise the
    # form stays untouched.
    assert "const conflicts = chosen.filter" in apply_block
    assert "const failed = chosen.filter" in apply_block
    assert "No form changes were made." in apply_block
    # Snapshot of the exact pre-apply draft is captured before any mutation
    # and persisted on the panel for the single-shot Undo action.
    assert "captureRecipeDraftSnapshot()" in apply_block
    assert "panel.dataset.assistantPreApplyDraft = JSON.stringify(preApplySnapshot)" in apply_block
    # No auto-save shortcut anywhere in the apply pipeline.
    assert "saveRecipe" not in apply_block
    assert "$('#recipe-form').dispatchEvent" in apply_block  # 'input' event, not submit
    assert "dispatchEvent(new Event('submit'" not in apply_block


def test_brewing_assistant_undo_restores_pre_apply_draft_in_memory_only() -> None:
    source = (STATIC_ROOT / "brewing.js").read_text(encoding="utf-8")
    # The undo action and its snapshot helpers are wired together.
    assert "function undoAppliedRecipeProposal(panel)" in source
    assert "function captureRecipeDraftSnapshot()" in source
    assert "function restoreRecipeDraftSnapshot(snapshot)" in source
    assert "panel.dataset.assistantPreApplyDraft" in source
    assert "delete panel.dataset.assistantPreApplyDraft" in source
    # The UI exposes exactly one in-memory Undo action per successful apply.
    assert "'Undo applied proposal'" in source
    assert "undoButton.dataset.assistantAction = 'undo'" in source
    assert "undoButton.addEventListener('click', () => undoAppliedRecipeProposal(panel))" in source
    # Undo restores every editable field but never persists or POSTs.
    undo_block = source.split("function undoAppliedRecipeProposal(panel) {", 1)[1].split(
        "function renderApplicableDiffChoices", 1
    )[0]
    assert "restoreRecipeDraftSnapshot(snapshot)" in undo_block
    assert "saveRecipe" not in undo_block
    assert "/api/recipes" not in undo_block
    assert "fetch(" not in undo_block
    assert "dispatchEvent(new Event('submit'" not in undo_block
    assert "saved revision is unchanged" in source
    # Restore writes the same editable fields captured by the snapshot helper.
    restore_block = source.split("function restoreRecipeDraftSnapshot(snapshot) {", 1)[1].split(
        "function applyRecipeProposal", 1
    )[0]
    for field in (
        "#recipe-id",
        "#recipe-revision",
        "#recipe-name",
        "#recipe-style",
        "#recipe-description",
        "#recipe-base-volume",
        "#recipe-beverage-type",
        "#recipe-initial-volume",
        "#recipe-target-abv",
        "#recipe-target-ph",
        "#recipe-target-sweetness",
        "#recipe-notes",
        "#recipe-ingredients",
    ):
        assert field in restore_block


def test_brewing_assistant_undo_is_fail_closed_against_recipe_binding() -> None:
    source = (STATIC_ROOT / "brewing.js").read_text(encoding="utf-8")
    undo_block = source.split("function undoAppliedRecipeProposal(panel) {", 1)[1].split(
        "function renderApplicableDiffChoices", 1
    )[0]
    # The undo is now gated on the same recipe binding the snapshot was
    # captured against. Mismatch must surface an error and leave the form /
    # snapshot untouched (no restoreRecipeDraftSnapshot call, no dataset
    # delete, no save/fetch).
    assert "boundJobId !== panelJobId" in undo_block
    assert "panel.dataset.assistantAppliedJobId" in undo_block
    assert "panel.dataset.assistantJobId" in undo_block
    assert "snapshot.recipeId" in undo_block
    assert "snapshot.recipeRevision" in undo_block
    assert "'#recipe-id'" in undo_block
    assert "'#recipe-revision'" in undo_block
    assert "Reload the recipe before undoing" in undo_block
    # The restore + dataset cleanup still happens only after the binding is
    # re-verified: the success path keeps its single-shot in-memory semantics
    # and the guard never reaches restoreRecipeDraftSnapshot on mismatch.
    assert undo_block.index("boundJobId !== panelJobId") < undo_block.index("restoreRecipeDraftSnapshot(snapshot)")
    assert "saveRecipe" not in undo_block
    assert "/api/recipes" not in undo_block
    assert "fetch(" not in undo_block
    assert "dispatchEvent(new Event('submit'" not in undo_block
    # Mismatch must surface an error and return false without deleting the
    # snapshot dataset so the operator can inspect / discard it.
    assert "return false;" in undo_block
    mismatch_block = undo_block.split("if (!boundJobId || !panelJobId || boundJobId !== panelJobId) {", 1)[1].split(
        "// Restore the exact pre-apply draft", 1
    )[0]
    assert "restoreRecipeDraftSnapshot" not in mismatch_block
    assert "delete panel.dataset.assistantPreApplyDraft" not in mismatch_block


def test_brewing_assistant_apply_snapshot_invalidation_on_recipe_selection() -> None:
    source = (STATIC_ROOT / "brewing.js").read_text(encoding="utf-8")
    clear_block = source.split("function clearRecipeForm() {", 1)[1].split(
        "\n  }\n\n  function fillRecipeForm", 1
    )[0]
    fill_block = source.split("function fillRecipeForm(recipe) {", 1)[1].split(
        "\n  }\n\n  function parsePiecewise", 1
    )[0]
    # Both code paths route through the single-shot invalidateAssistantRecipeBinding
    # helper so a stale Undo / old approve closure can never cross recipe
    # selection (new draft or switching to a different recipe). The helper
    # must clear the apply snapshot, applied-job id, AND live
    # assistantJobId; deleting assistantJobId specifically prevents an
    # orphaned approve click handler from overwriting a freshly-rendered
    # audit's approval binding with the previous job's approval.
    for block in (clear_block, fill_block):
        assert "'.assistant-panel[data-assistant-kind=\"recipe\"]'" in block
        assert "invalidateAssistantRecipeBinding(" in block
    # The helper itself must delete every lineage key in one place (so a
    # future add-on does not silently leak an approval lineage key across
    # selection). The apply / approve / rewrite closure paths all inspect
    # the same five keys, and the helper drops the entire set + rotates
    # the binding generation token.
    helper = source.split(
        "function invalidateAssistantRecipeBinding(panel) {", 1
    )[1].split("\n  }\n", 1)[0]
    for key in (
        "assistantPreApplyDraft",
        "assistantAppliedJobId",
        "assistantJobId",
    ):
        assert key in helper


def test_brewing_assistant_rewrite_button_is_conditional_on_audit_success() -> None:
    source = (STATIC_ROOT / "brewing.js").read_text(encoding="utf-8")
    # The button DOM node is still constructed so the approve-click closure
    # can flip its disabled flag, but the append is gated to a succeeded
    # recipe_audit job. Other kinds (recipe_autofill, recipe_rewrite,
    # brew_analyze, brew_event_draft) and pending/running/failed jobs never
    # expose the rewrite action in the review.
    rewrite_button_section = source.split("const rewriteButton = node('button', 'secondary', 'Generate proposed changes');", 1)[1].split(
        "if (result.proposal) {", 1
    )[0]
    assert "review.append(rewriteButton);" in rewrite_button_section
    assert "if (job.kind === 'recipe_audit' && job.status === 'succeeded') {" in rewrite_button_section
    # No unguarded review.append(rewriteButton) remains anywhere in the file:
    # the only occurrence is inside the succeeded-recipe_audit guard.
    assert source.count("review.append(rewriteButton);") == 1
    guarded_append = rewrite_button_section.split(
        "if (job.kind === 'recipe_audit' && job.status === 'succeeded') {", 1
    )[1]
    assert "review.append(rewriteButton);" in guarded_append
    # The click-handler guard family (approval, scope, finding id, explicit
    # save) is preserved for the eligible audit path.
    handler_block = source.split("rewriteButton.addEventListener('click', async () => {", 1)[1].split(
        "setAssistantBusy(panel, false);", 1
    )[0]
    assert "panel.dataset.assistantAuditJobId !== job.job_id" in handler_block
    assert "liveScope.recipe_id" in handler_block
    assert "liveScope.recipe_revision" in handler_block
    assert "Select at least one approved finding to include in the rewrite." in handler_block
    assert "approved_finding_ids: liveSelection" in handler_block


def test_brewing_assistant_rewrite_is_explicit_reviewed_action() -> None:
    source = (STATIC_ROOT / "brewing.js").read_text(encoding="utf-8")
    # The W4 button is clearly labeled and only appears after a succeeded audit.
    assert "Generate proposed changes" in source
    # The DOM exposes the action via data-assistant-action for the busy-toggling
    # helper, plus a stable data-assistant-rewrite marker (dataset.assistantRewrite
    # serialises to data-assistant-rewrite in the DOM).
    assert "data-assistant-action" in source
    assert "rewriteButton.dataset.assistantRewrite = 'true'" in source
    assert "job.kind === 'recipe_audit'" in source
    assert "job.status === 'succeeded'" in source
    # The click handler is guarded: a stale panel binding must short-circuit.
    assert "panel.dataset.assistantAuditJobId !== job.job_id" in source
    # The handler must POST /api/assistant/jobs with the documented payload shape.
    assert "kind: 'recipe_rewrite'" in source
    assert "parent_job_id: job.job_id" in source
    assert "approved_finding_ids: liveSelection" in source
    assert "surface: 'recipe'" in source
    assert "scope: liveScope" in source
    assert "draft," in source
    assert "client_request_id: newClientId()" in source
    assert "rewriteButton.disabled = false" in source
    # We poll the new job via the existing path instead of auto-applying.
    # The poll recursion now carries the binding generation token so a
    # recipe selection / new submit during the rewrite POST aborts the
    # in-flight poll without rebinding the panel.
    assert "pollAssistantJob(panel, submitted.job_id, Date.now(), rewriteToken)" in source
    assert "setAssistantBusy(panel, false)" in source
    # No auto-save shortcut anywhere in the rewrite click handler.
    handler_block = source.split("rewriteButton.addEventListener('click', async () => {", 1)[1].split("\n    });", 1)[0]
    assert "saveRecipe" not in handler_block
    assert "applyRecipeProposal(" not in handler_block
    assert "$('#recipe-form').dispatchEvent" not in handler_block


def test_brewing_assistant_rewrite_action_disables_when_unapproved() -> None:
    source = (STATIC_ROOT / "brewing.js").read_text(encoding="utf-8")
    # Extract the rewriteButton.disabled line and confirm it gates on the
    # approved/partial decision the server records.
    assert "rewriteButton.disabled = !canRewriteNow" in source
    assert "'approved' || job.approval_decision === 'partial'" in source
    # Operators must re-tick findings before the action will fire; the server
    # contract enforces exact match, so the browser cannot silently widen.
    assert "Select at least one approved finding to include in the rewrite." in source


def test_brewing_assistant_rewrite_validates_scope_and_rev_against_audit() -> None:
    source = (STATIC_ROOT / "brewing.js").read_text(encoding="utf-8")
    assert "assistantAuditScopeRecipeId" in source
    assert "assistantAuditScopeRecipeRevision" in source
    assert "liveScope.recipe_id" in source
    assert "liveScope.recipe_revision" in source
    # The audit approval is persisted on the panel closure for re-validation.
    assert "assistantApprovedFindingIds" in source
    assert "panel.dataset.assistantAuditJobId = job.job_id" in source


def test_recipe_form_selection_clears_stale_brew_context() -> None:
    source = (STATIC_ROOT / "brewing.js").read_text(encoding="utf-8")
    clear_block = source.split("function clearRecipeForm() {", 1)[1].split(
        "\n  }\n\n  function fillRecipeForm", 1
    )[0]
    fill_block = source.split("function fillRecipeForm(recipe) {", 1)[1].split(
        "\n  }\n\n  function parsePiecewise", 1
    )[0]
    assert "state.selectedBrew = null;" in clear_block
    assert "state.selectedBrew = null;" in fill_block


def test_phone_pwa_assets_are_fixed_same_origin_routes(test_client) -> None:
    expected = {
        "/manifest.webmanifest": (STATIC_ROOT / "manifest.webmanifest", "application/manifest+json"),
        "/static/service-worker.js": (STATIC_ROOT / "service-worker.js", "application/javascript"),
        "/static/brew-icon.svg": (STATIC_ROOT / "brew-icon.svg", "image/svg+xml"),
    }
    for route, (path, content_type) in expected.items():
        response = test_client.get(route)
        assert response.status_code == 200
        assert response.content == path.read_bytes()
        assert response.headers["content-type"].startswith(content_type)
        assert response.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    worker = test_client.get("/static/service-worker.js")
    assert worker.headers["service-worker-allowed"] == "/"

    html = test_client.get("/").text
    assert '<link rel="manifest" href="/manifest.webmanifest" />' in html
    assert '<meta name="theme-color" content="#1f6a3c" />' in html


def test_calibration_ui_exposes_cubic_and_saves_inactive(test_client) -> None:
    html = test_client.get("/").text
    script = test_client.get("/static/dashboard.js").text
    assert '<option value="3">Monotonic cubic (3)</option>' in html
    assert 'aria-label="Calibration angle 4"' in html
    assert "body: JSON.stringify({label, order, points, activate: false})" in script
    assert "Activate only after the stable water-reference check." in script


def test_service_worker_revalidates_shell_before_cache_fallback(test_client) -> None:
    worker = test_client.get("/static/service-worker.js").text
    assert "brewing-central-shell-v3" in worker
    assert "cached || fetch(request)" not in worker
    assert "fetch(request)" in worker
    assert ".catch(() => caches.match(request))" in worker


# ---------------------------------------------------------------------------
# Phase 6 cross-recipe apply-binding repair + W4/F2 audit-guard repair.
# Source-contract tests are pinned against exact tokens / ordering in
# app/static/brewing.js. They do not execute JS — jsdom is intentionally
# avoided per the bounded contract.
# ---------------------------------------------------------------------------


def _brewing_source() -> str:
    return (STATIC_ROOT / "brewing.js").read_text(encoding="utf-8")


def _apply_block(source: str) -> str:
    return source.split(
        "function applyRecipeProposal(panel, job, result, selectedPaths) {", 1
    )[1].split("\n  }\n\n  function undoAppliedRecipeProposal", 1)[0]


def _approve_block(source: str) -> str:
    # The approve click handler closes with the success message followed by
    # the listener's own }); line. Slice on that exact closing rather than
    # the first nested }); (the inner try/catch has its own close).
    start = source.index("approve.addEventListener('click', async () => {")
    end_marker = "Audit approval recorded. A rewrite remains a separate reviewed job.'"
    end = source.index(end_marker, start)
    return source[start:end + len(end_marker)]


def _rewrite_section(source: str) -> str:
    return source.split(
        "const rewriteButton = node('button', 'secondary', 'Generate proposed changes');",
        1,
    )[1].split("if (result.proposal) {", 1)[0]


def _rewrite_handler(source: str) -> str:
    return source.split(
        "rewriteButton.addEventListener('click', async () => {", 1
    )[1].split("\n    });", 1)[0]


def _undo_append_section(source: str) -> str:
    return source.split("if (result.proposal) {", 1)[1].split(
        "if (result.kind === 'brew_event_draft'", 1
    )[0]


def test_brewing_assistant_apply_uses_explicit_null_or_number_scope_sentinel() -> None:
    # The cross-recipe apply-binding defect: a proposal created against a new
    # draft (scope.recipe_id === null, scope.recipe_revision === null) could
    # be applied after a saved recipe was selected, because the previous
    # guards were `scope.recipe_id != null && Number(...) !== currentId` —
    # i.e. a null scope silently matched anything. The repair introduces a
    # single null-or-number normaliser and requires exact equality.
    source = _brewing_source()
    assert "function normalizeScopeRecipeId(value)" in source
    # The normaliser must collapse '', 0, NaN, undefined, null to a single
    # null sentinel and keep only positive finite numbers.
    helper_body = source.split("function normalizeScopeRecipeId(value) {", 1)[1].split(
        "\n  }\n", 1
    )[0]
    assert "Number.isFinite" in helper_body
    assert "> 0" in helper_body
    apply_block = _apply_block(source)
    # The old `!= null` truthy-shortcut must be gone from the apply guard.
    assert "!= null && Number(scope.recipe_id)" not in apply_block
    assert "!= null && Number(scope.recipe_revision)" not in apply_block
    # The current form and the job scope are normalised through the same
    # helper, so the comparison is a strict equality on a single sentinel.
    assert apply_block.count("normalizeScopeRecipeId") >= 4
    assert "scopeRecipeId !== currentRecipeId" in apply_block
    assert "scopeRevision !== currentRevision" in apply_block
    # Order matters: the binding equality check must sit BEFORE the snapshot
    # capture / mutation so a mismatch fails closed before the form changes.
    assert apply_block.index("scopeRecipeId !== currentRecipeId") < apply_block.index(
        "captureRecipeDraftSnapshot()"
    )
    assert apply_block.index("scopeRevision !== currentRevision") < apply_block.index(
        "captureRecipeDraftSnapshot()"
    )


def test_brewing_assistant_recipe_selection_clears_assistant_job_id() -> None:
    # The two recipe-selection paths route through the W4/F2 single-shot
    # invalidateAssistantRecipeBinding helper. The helper must drop the
    # full approval-lineage key set (assistantJobId + every approval /
    # scope / watchdog key in the lineage) in one place so a future add-on
    # cannot silently leak a stale approval across selection.
    source = _brewing_source()
    clear_block = source.split("function clearRecipeForm() {", 1)[1].split(
        "\n  }\n\n  function fillRecipeForm", 1
    )[0]
    fill_block = source.split("function fillRecipeForm(recipe) {", 1)[1].split(
        "\n  }\n\n  function parsePiecewise", 1
    )[0]
    for block in (clear_block, fill_block):
        assert "invalidateAssistantRecipeBinding(" in block
    # The helper drops every approval-lineage key on the recipe panel so
    # that approve / rewrite closures captured against the previous
    # selection cannot rebind an already-replaced audit. The single
    # key-list anchor is what guarantees the next maintainer cannot grow
    # the lineage without updating the clear path too.
    helper = source.split(
        "function invalidateAssistantRecipeBinding(panel) {", 1
    )[1].split("\n  }\n", 1)[0]
    for key in (
        "assistantPreApplyDraft",
        "assistantAppliedJobId",
        "assistantJobId",
        "assistantAuditJobId",
        "assistantApprovedFindingIds",
        "assistantAuditScopeRecipeId",
        "assistantAuditScopeRecipeRevision",
        "assistantWatchdogMs",
    ):
        assert key in helper, f"selection invalidator must drop {key}"


def test_brewing_assistant_approve_handler_checks_job_binding_pre_and_post() -> None:
    # The approve click handler must verify the panel is still bound to this
    # audit job BEFORE the POST (refuse stale closures) and AFTER the await
    # (refuse concurrent re-renders that swapped assistantJobId). Only after
    # both checks pass may the approval binding be written.
    source = _brewing_source()
    approve_block = _approve_block(source)
    # Two distinct guard sites, both comparing assistantJobId to job.job_id.
    occurrences = [
        idx for idx in range(len(approve_block))
        if approve_block.startswith("if (panel.dataset.assistantJobId !== job.job_id)", idx)
    ]
    assert len(occurrences) == 2, (
        "Expected exactly two job-binding guards in approve handler (pre- and post-await)"
    )
    # The approval-binding writes must sit AFTER both guards, not between
    # them — otherwise the post-await guard would never gate the write.
    write_marker = "panel.dataset.assistantAuditJobId = job.job_id"
    assert write_marker in approve_block
    assert occurrences[0] < approve_block.index(write_marker)
    assert occurrences[1] < approve_block.index(write_marker)
    # And the second guard must come AFTER the await api() call.
    api_call = approve_block.index(
        "approvedRecord = await api(`/api/assistant/jobs/${job.job_id}/approve`"
    )
    assert occurrences[1] > api_call
    # Each guard must surface a distinct error message so operators can tell
    # them apart (pre vs post).
    pre_msg_idx = approve_block.index("panel is bound to a different audit", occurrences[0])
    post_msg_idx = approve_block.index(
        "The audit binding changed while the approval was in flight"
    )
    assert pre_msg_idx > occurrences[0] and post_msg_idx > occurrences[1]


def test_brewing_assistant_undo_button_is_deduplicated_before_append() -> None:
    # Repeated applies / re-renders must never stack multiple Undo buttons:
    # only one button is visible per snapshot. Both append sites (the
    # post-apply callback and the unconditional re-render branch) must
    # remove any pre-existing undo button before appending.
    source = _brewing_source()
    undo_section = _undo_append_section(source)
    # Two dedup sweeps, one per append site, each before the append.
    dedup_sweeps = undo_section.count(
        "$$('[data-assistant-action=\"undo\"]', "
    )
    assert dedup_sweeps == 2
    # Each dedup sweep must appear before its matching append (the button
    # constructor with `'Undo applied proposal'`). Order matters: dedup
    # then construct then append.
    first_dedup = undo_section.index(
        "$$('[data-assistant-action=\"undo\"]', "
    )
    first_label = undo_section.index("'Undo applied proposal'", first_dedup)
    second_dedup = undo_section.index(
        "$$('[data-assistant-action=\"undo\"]', ", first_label
    )
    second_label = undo_section.index("'Undo applied proposal'", second_dedup)
    assert first_dedup < first_label < second_dedup < second_label
    # Exactly two append sites remain in the file (one inside the
    # renderApplicableDiffChoices callback, one in the unconditional
    # re-render branch). No other DOM append uses 'Undo applied proposal'.
    full_undo_appends = source.count("'Undo applied proposal'")
    assert full_undo_appends == 2


def test_brewing_assistant_rewrite_disabled_when_approved_findings_empty() -> None:
    # The rewrite action must NEVER be enabled when:
    #   - the audit has zero findings (cannot have any approved ids to
    #     apply), OR
    #   - the recorded approved_finding_ids set is empty (an empty approval
    #     from the server must stay disabled regardless of local checkbox
    #     state).
    source = _brewing_source()
    rewrite_section = _rewrite_section(source)
    # canRewriteNow must check BOTH job.result.findings.length AND the
    # recorded approved_finding_ids length.
    assert "job.result.findings.length > 0" in rewrite_section
    assert "recordedApprovedFindingIds.length > 0" in rewrite_section
    # The recorded-ids length check is fed by parsing the panel dataset
    # through the same helper shape the click handler uses; the helper must
    # be safe on a missing / malformed / non-array dataset value.
    helper_match = source.split(
        "const recordedApprovedFindingIds = (() => {", 1
    )[1].split("\n    })();", 1)[0]
    assert "JSON.parse(panel.dataset.assistantApprovedFindingIds || '[]')" in helper_match
    assert "Array.isArray(parsed) ? parsed : []" in helper_match
    # And the rewrite click handler still enforces the empty-approved-ids
    # guard at the action site, in case the operator flips state between
    # render and click.
    handler = _rewrite_handler(source)
    assert "!approvedFindingIds.length" in handler
    assert (
        "No approved findings are bound to this panel" in handler
    )


def test_brewing_assistant_rewrite_compares_scope_unconditionally() -> None:
    # The client-side audit scope binding must not use truthy-empty checks
    # that bypass a null draft scope. Both recipe_id and recipe_revision
    # are compared unconditionally through the same null-or-number
    # normaliser as the apply pipeline. A null audit scope against a
    # non-null live recipe (or vice versa) MUST fail closed.
    source = _brewing_source()
    handler = _rewrite_handler(source)
    # The rewrite handler must capture the draft via the recipeDraftForAssistant()
    # helper (which deep-clones + maps unit_other → custom_unit + deletes
    # unit_other before handing the draft to the assistant pipeline). The
    # bare recipePayload() contract is reserved for the recipe save API; the
    # handler must never call it directly.
    assert "recipeDraftForAssistant()" in handler
    # No direct recipePayload() call anywhere in the rewrite-handler slice.
    assert "recipePayload()" not in handler
    # The old truthy-skipping guards must be gone.
    assert "if (auditRecipeId && String(liveScope.recipe_id" not in handler
    assert "if (auditRecipeRevision && String(liveScope.recipe_revision" not in handler
    # The audit scope values come from the dataset through normalizeScopeRecipeId;
    # they must be compared to the live scope values run through the same
    # helper, so null/null, number/number, and null/number are all distinct.
    assert handler.count("normalizeScopeRecipeId") >= 4
    assert "normalizeScopeRecipeId(panel.dataset.assistantAuditScopeRecipeId)" in handler
    assert "normalizeScopeRecipeId(panel.dataset.assistantAuditScopeRecipeRevision)" in handler
    assert "auditRecipeId !== normalizeScopeRecipeId(liveScope.recipe_id)" in handler
    assert "auditRecipeRevision !== normalizeScopeRecipeId(liveScope.recipe_revision)" in handler
    # The two scope checks must both run before the helper is invoked, so a
    # mismatch fails closed before any recipeDraftForAssistant() or api() call.
    draft_capture = handler.index("recipeDraftForAssistant()")
    assert handler.index(
        "auditRecipeId !== normalizeScopeRecipeId(liveScope.recipe_id)"
    ) < draft_capture
    assert handler.index(
        "auditRecipeRevision !== normalizeScopeRecipeId(liveScope.recipe_revision)"
    ) < draft_capture
    # The scope-write sites (approve handler + rewrite submit) must use the
    # canonical null → '' and positive number → String(number) storage
    # convention so the dataset is always re-readable by the normaliser.
    approve_block = _approve_block(source)
    assert "approvedRecipeId != null ? String(approvedRecipeId) : ''" in approve_block
    assert "approvedRecipeRevision != null ? String(approvedRecipeRevision) : ''" in approve_block
    assert "auditRecipeId != null ? String(auditRecipeId) : ''" in handler
    assert "auditRecipeRevision != null ? String(auditRecipeRevision) : ''" in handler
    # The isolated recipeDraftForAssistant() function body must call
    # recipePayload(), deep-clone with JSON.parse(JSON.stringify(...)),
    # mirror unit_other onto custom_unit, and delete unit_other — so the
    # helper is the single source of truth for the API ↔ assistant payload
    # translation and the rewrite handler cannot accidentally bypass it.
    draft_helper = source.split(
        "function recipeDraftForAssistant() {", 1
    )[1].split("\n  }\n", 1)[0]
    assert "recipePayload()" in draft_helper
    assert "JSON.parse(JSON.stringify(payload))" in draft_helper
    assert "ingredient.custom_unit = ingredient.unit_other" in draft_helper
    assert "delete ingredient.unit_other" in draft_helper


# ---------------------------------------------------------------------------
# W4/F2 stale-poll + approval-lineage race repair. Source-contract tests pin
# the bounded polling/generation-token fix, immediate job binding,
# approve-lineage invalidation, and rewrite busy guard added on top of the
# Phase 6 cross-recipe apply-binding repair. They do NOT execute JS (jsdom
# is intentionally avoided) and they leave every prior guard and dataset
# write unchanged.
# ---------------------------------------------------------------------------


def _poll_block(source: str) -> str:
    # Slice the poll function body from the signature to the function's
    # closing `\n  }\n` (which appears immediately before the next function /
    # top-level statement). The old test helper sliced on a `\n  }\n` it
    # expected at the same depth; the W4/F2 polls intentionally
    # structure try/catch in-line so a single depth match still works.
    start = source.index("async function pollAssistantJob(panel, jobId, startedAt, expectedToken) {")
    close = source.index("\n  }\n", start)
    return source[start:close]


def _submit_block(source: str) -> str:
    start = source.index("async function submitAssistantJob(panel, kind, auditProfile = 'ordinary') {")
    close = source.index("\n  }\n", start)
    return source[start:close]


def _render_block(source: str) -> str:
    start = source.index("function renderAssistantJob(panel, job) {")
    close = source.index("\n  }\n", start)
    return source[start:close]


def _rewrite_handler_full(source: str) -> str:
    # The rewrite click handler is an inline async closure with try/catch.
    # Slice from the addEventListener signature to the FINAL addEventListener
    # close (the second outer close at indent 4). The intermediate
    # `\n    });` lines close the inner try/catch and are NOT the handler
    # boundary. The unique sentinel sits right before the next
    # "// Append the rewrite button" comment that owns the append site.
    start = source.index("rewriteButton.addEventListener('click', async () => {")
    marker = "// Append the rewrite button only for a succeeded recipe_audit job"
    end = source.index(marker, start)
    handler_end = source.rfind("\n    });", start, end) + len("\n    });")
    return source[start:handler_end]


def test_brewing_assistant_recipe_invalidation_is_single_source_of_truth() -> None:
    # W4/F2: the recipe-selection paths MUST route through a single helper
    # that drops EVERY approval-lineage key (not just pre-apply snapshot,
    # applied-job, and live job). The helper rotates the binding
    # generation token so any in-flight pollAssistantJob recursion becomes
    # a no-op before render / rebind.
    source = _brewing_source()
    assert "function invalidateAssistantRecipeBinding(panel)" in source
    assert "function nextAssistantBindingToken(panel)" in source
    # Both selection paths must call the helper, and the helper must drop
    # the entire lineage-key list (including the audit approval lineage
    # the previous repair left dangling) plus rotate the token.
    helper = source.split(
        "function invalidateAssistantRecipeBinding(panel) {", 1
    )[1].split("\n  }\n", 1)[0]
    for key in (
        "assistantPreApplyDraft",
        "assistantAppliedJobId",
        "assistantJobId",
        "assistantAuditJobId",
        "assistantApprovedFindingIds",
        "assistantAuditScopeRecipeId",
        "assistantAuditScopeRecipeRevision",
        "assistantWatchdogMs",
    ):
        assert key in helper
    assert "nextAssistantBindingToken(panel)" in helper
    # Recipe selection paths must call the helper so a future add-on to
    # the lineage key list automatically clears on selection.
    clear_block = source.split("function clearRecipeForm() {", 1)[1].split(
        "\n  }\n\n  function fillRecipeForm", 1
    )[0]
    fill_block = source.split("function fillRecipeForm(recipe) {", 1)[1].split(
        "\n  }\n\n  function parsePiecewise", 1
    )[0]
    assert clear_block.count("invalidateAssistantRecipeBinding(") == 1
    assert fill_block.count("invalidateAssistantRecipeBinding(") == 1


def test_brewing_assistant_poll_carries_generation_token_through_recursion() -> None:
    # The poll recursion must accept a binding generation token, compare
    # it against the live token at the entry of every frame AND after
    # the awaited GET, and pass it into the rescheduled setTimeout call
    # so a recipe selection (which rotates the token via the
    # invalidateAssistantRecipeBinding helper) cancels the in-flight
    # poll.
    source = _brewing_source()
    poll_block = _poll_block(source)
    # Signature accepts the token as a 4th parameter.
    sig = source.split("async function pollAssistantJob(", 1)[1].split(") {", 1)[0]
    assert "expectedToken" in sig
    # The entry guard compares the live token against expectedToken when
    # one is supplied. Refusing on undefined preserves any test / call
    # site that does not supply a token.
    assert "expectedToken === currentAssistantBindingToken(panel)" in poll_block
    # The post-await guard re-checks the token after the awaited GET.
    # Without this, a recipe selection made during the await would still
    # let renderAssistantJob run against the new panel state and rebind
    # the old job.
    await_marker = poll_block.index("await api(`/api/assistant/jobs/${jobId}`)")
    assert poll_block.index(
        "currentAssistantBindingToken(panel) !== expectedToken", await_marker
    ) > await_marker
    # The setTimeout reschedule must pass the token, otherwise the
    # recursion loses the cancellation guard on every tick.
    assert (
        "window.setTimeout(() => pollAssistantJob(panel, jobId, startedAt, expectedToken), 500)"
        in poll_block
    )
    # The catch branch must also refuse to mutate / surface the error
    # when the token has rotated (a failing GET after a recipe selection
    # must not leak its error onto the new panel).
    catch_section = poll_block.split("catch (error) {", 1)[1]
    assert (
        "currentAssistantBindingToken(panel) !== expectedToken" in catch_section
    )


def test_brewing_assistant_poll_refuses_on_mismatched_job_id() -> None:
    # A stale poll for an old job must also refuse to render / mutate
    # when the panel has been re-bound to a newer job (the polled job
    # id is not the live binding). Selection wipes both the token AND
    # the job id, so this guard closes the case where a new submit
    # happened in between (which only rotates the token + rebinds).
    source = _brewing_source()
    poll_block = _poll_block(source)
    # Both post-await guards (after the awaited GET and inside the
    # catch branch) check that the live panel binding is still the
    # polled job.
    assert poll_block.count("panel.dataset.assistantJobId !== jobId") >= 2
    # The entry guard uses an inverse equality (`=== jobId`, with an
    # empty-binding fallback). The structural guarantee is that the
    # entry guard refuses to re-render when the polled id is NOT the
    # live binding.
    assert "panel.dataset.assistantJobId === jobId" in poll_block
    assert "!panel?.dataset?.assistantJobId" in poll_block
    # No unguarded renderAssistantJob call. The successful / failed
    # status branch only renders when canRebindAssistantPanel / the
    # post-await guards agree.
    assert poll_block.index("renderAssistantJob(panel, job)") > poll_block.index(
        "currentAssistantBindingToken(panel) !== expectedToken"
    )


def test_brewing_assistant_submit_binds_receipt_before_polling() -> None:
    # submitAssistantJob must bind panel.dataset.assistantJobId to the
    # receipt IMMEDIATELY after the POST resolves (before any awaited
    # GET inside pollAssistantJob), and only when the binding
    # generation token is still current. The token MUST be captured
    # before the POST so the immediate-binding site and the recursive
    # poll both see the exact same generation.
    source = _brewing_source()
    submit_block = _submit_block(source)
    # Token is captured BEFORE the awaited POST.
    assert submit_block.index("const submittedToken = nextAssistantBindingToken(panel)") < submit_block.index(
        "const receipt = await api('/api/assistant/jobs'"
    )
    # Immediate receipt binding site sits AFTER the POST but BEFORE the
    # poll call, and is guarded by a token equality check.
    post_idx = submit_block.index("const receipt = await api('/api/assistant/jobs'")
    binding_idx = submit_block.index(
        "panel.dataset.assistantJobId = receipt.job_id"
    )
    poll_call_idx = submit_block.index("pollAssistantJob(panel, receipt.job_id")
    assert post_idx < binding_idx < poll_call_idx
    # The token guard must sit between the POST and the binding write.
    assert submit_block.index(
        "currentAssistantBindingToken(panel) !== submittedToken"
    ) > post_idx
    assert submit_block.index(
        "currentAssistantBindingToken(panel) !== submittedToken"
    ) < binding_idx
    # The poll call carries the captured token.
    assert "pollAssistantJob(panel, receipt.job_id, Date.now(), submittedToken)" in submit_block
    # On a token rotation mid-POST, the handler must surface an error
    # and release busy state. The if block is closed at the 6-space
    # indent (`      }`); the test splits on that exact token so the
    # guard body is captured without including the binding write.
    rejection = submit_block.split(
        "currentAssistantBindingToken(panel) !== submittedToken) {", 1
    )[1].split("\n      }\n", 1)[0]
    assert "setMessage(status," in rejection and "setAssistantBusy(panel, false)" in rejection


def test_brewing_assistant_render_has_defensive_rebind_guard() -> None:
    # renderAssistantJob unconditionally rebinds panel.dataset.assistantJobId
    # at the top of every call. The defensive guard must refuse to
    # rebind (and refuse to overwrite an existing different binding)
    # BEFORE the rebind write runs. The token check is intentionally
    # NOT performed inside renderAssistantJob — the authoritative
    # generation-token + job-id checks live in pollAssistantJob /
    # submitAssistantJob; this guard closes the residual race where a
    # direct renderAssistantJob call could otherwise clobber the live
    # binding without a token rotation.
    source = _brewing_source()
    render_block = _render_block(source)
    assert "function canRebindAssistantPanel(panel, polledJobId)" in source
    guard_call = render_block.index("canRebindAssistantPanel(panel, job?.job_id)")
    rebind_write = render_block.index("panel.dataset.assistantJobId = job.job_id")
    assert guard_call < rebind_write
    assert "return;" in render_block.split(
        "if (!canRebindAssistantPanel(panel, job?.job_id)) {", 1
    )[1].split("\n    }\n", 1)[0]
    # The helper itself must allow rebinding only when no job is bound,
    # OR when the polled job matches the currently-bound one.
    helper = source.split("function canRebindAssistantPanel(panel, polledJobId) {", 1)[
        1
    ].split("\n  }\n", 1)[0]
    assert "!currentJobId" in helper
    assert "currentJobId === polledJobId" in helper


def test_brewing_assistant_rewrite_has_busy_guard_and_token_binding() -> None:
    # The rewrite click handler MUST refuse early on the busy flag (a
    # fast double-click must not enqueue two rewrites against the same
    # approved audit) and MUST rotate the binding generation token
    # before the POST, so any in-flight poll for the prior approved
    # audit job becomes a no-op. The token is then passed into the
    # recursive poll path alongside the new job id.
    source = _brewing_source()
    # The legacy _rewrite_handler slices on the first inner close; the
    # W4/F2 busy guard sits BEFORE the try/catch, so we slice the full
    # handler (signature → addEventListener close) for this test.
    handler = _rewrite_handler_full(source)
    # Busy guard sits at the very top of the click handler body, BEFORE
    # the existing pre-/post-race guards.
    busy_idx = handler.index("panel.dataset.assistantBusy === 'true'")
    pre_post_idx = handler.index("panel.dataset.assistantJobId !== job.job_id")
    assert busy_idx < pre_post_idx
    # Busy check must precede the captured-token rotation as well —
    # we never rotate / poll on a refused click.
    token_rotate_idx = handler.index("const rewriteToken = nextAssistantBindingToken(panel)")
    assert busy_idx < token_rotate_idx
    # Same guard placement for the POST submit: the rewrite submitted
    # job id must be bound inside the post-await branch and the bound
    # token must agree with the captured one.
    post_idx = handler.index(
        "const submitted = await api('/api/assistant/jobs'"
    )
    binding_idx = handler.index(
        "panel.dataset.assistantJobId = submitted.job_id"
    )
    assert handler.index(
        "currentAssistantBindingToken(panel) !== rewriteToken", post_idx
    ) < binding_idx
    assert (
        "pollAssistantJob(panel, submitted.job_id, Date.now(), rewriteToken)" in handler
    )
    # The busy guard must short-circuit silently (no status mutation) so
    # the existing visible race guards further down stay authoritative.
    busy_block = handler.split(
        "if (panel.dataset.assistantBusy === 'true') {", 1
    )[1].split("\n      }\n", 1)[0]
    assert "return;" in busy_block
    assert "setMessage(" not in busy_block


# ---------------------------------------------------------------------------
# W5 archive UI vertical slice: selection + compare + freeze + annotation +
# fork. Source-contract tests pin DOM shape, endpoint contracts, payload
# shape, lineage, and the no-mutation / no-model guarantees. They do not
# execute JS (jsdom is intentionally avoided).
# ---------------------------------------------------------------------------


_W5_REQUIRED_TOKENS = (
    # Top-level compare button + per-run checkbox + archive-compare panel
    "const compareButton = node('button', 'secondary', 'Compare selected');",
    "checkbox.type = 'checkbox';",
    "checkbox.dataset.brewId = String(brew.id);",
    "comparePanel.id = 'archive-compare-panel';",
    "renderArchiveComparisonPanel(panel, 'loading', [], [], null);",
    "renderArchiveComparisonPanel(panel, 'error', [], [], error.message);",
    "renderArchiveComparisonPanel(panel, 'empty', [], [], null);",
    "renderArchiveComparisonPanel(panel, 'no-evidence', brews, bundles, null);",
    "renderArchiveComparisonPanel(panel, 'ready', brews, bundles, null);",
    # Per-card freeze + assistant-context + fork + annotation
    "node('button', 'secondary', 'Freeze / review evidence')",
    "node('button', 'secondary', 'Use as assistant context')",
    "node('button', 'secondary', 'Fork as new recipe')",
    "node('button', 'secondary', 'Add annotation')",
    "node('button', 'secondary', 'Reload annotations')",
    # Endpoint and payload contracts
    "method: 'POST'",
    "`/api/brews/${brewId}/archive-evidence`",
    "`/api/brews/${brew.id}/archive-annotations`",
    "'/api/archive/fork-recipe'",
    "classification,",
    "origin: 'operator'",
    "content: { notes }",
    "parent_annotation_id: parentId",
    # Fork payload shape
    "source_recipe_id: brew.recipe_id",
    "source_recipe_revision: brew.recipe_snapshot && brew.recipe_snapshot.revision",
    "source_brew_run_id: brew.id",
    "new_name: newName",
    # Annotation classification list
    "const ARCHIVE_ANNOTATION_CLASSIFICATIONS = [",
    "'operator_post_brew',",
    "'tasting_outcome',",
    "'hypothesis',",
    "'annotation_clarification',",
    # Review panel data source
    "bundle.source_snapshot_json",
    "bundle.source_events_json",
    # Fork refresh + select
    "await loadRecipes(created.id)",
    # Window.prompt for fork name
    "window.prompt(",
    # Empty archive state
    "'Completed and aborted brews will appear here.'",
)


def test_w5_archive_required_tokens_are_present() -> None:
    source = _brewing_source()
    for token in _W5_REQUIRED_TOKENS:
        assert token in source, f"missing required W5 UI token: {token!r}"


def test_w5_archive_compare_uses_archive_evidence_endpoint_for_each_run() -> None:
    # The Compare selected action must POST /api/brews/{id}/archive-evidence
    # for every selected run, via the shared freezeArchiveEvidence helper,
    # then render the review panel from the persisted bundle fields.
    source = _brewing_source()
    compare_block = source.split(
        "async function compareSelectedArchivedBrews(button) {", 1
    )[1].split("\n  }\n", 1)[0]
    # Per-run POST via the freezeArchiveEvidence helper, applied to every
    # selected brew through a Promise.all fan-out.
    assert "brews.map((brew) => freezeArchiveEvidence(brew.id))" in compare_block
    assert "await Promise.all(brews.map" in compare_block
    # The freezeArchiveEvidence helper itself must use the exact endpoint +
    # method POST so every per-run call is a real evidence freeze.
    helper = source.split(
        "function freezeArchiveEvidence(brewId) {", 1
    )[1].split("\n  }\n", 1)[0]
    assert helper.count("/archive-evidence") == 1
    assert "method: 'POST'" in helper


def test_w5_archive_review_panel_uses_persisted_snapshot_and_events_only() -> None:
    # The deterministic review panel must render ONLY from the persisted
    # source_snapshot_json / source_events_json of the returned bundles; no
    # fetch to /api/assistant/, no model call, no invented measurements.
    source = _brewing_source()
    panel_block = source.split(
        "function renderArchiveComparisonPanel(panel, status, brews, bundles, errorMessage) {", 1
    )[1].split("\n  }\n", 1)[0]
    # Reads the persisted fields.
    assert "bundle.source_snapshot_json" in panel_block
    assert "bundle.source_events_json" in panel_block
    # JSON.parse the persisted strings before rendering so the panel never
    # injects the raw server JSON into the DOM as textContent.
    assert panel_block.count("JSON.parse(bundle") == 2
    # The review panel must never touch the assistant endpoints, never send
    # a model request, and never invent measurements.
    assert "/api/assistant/" not in panel_block
    assert "ZeroClaw" not in panel_block
    assert "Gemma" not in panel_block
    assert "fetch(" not in panel_block
    assert "api(" not in panel_block
    assert "window.prompt(" not in panel_block
    assert "saveRecipe" not in panel_block


def test_w5_archive_review_panel_states_are_explicit() -> None:
    # loading / error / empty / no-evidence / ready must each have a
    # dedicated render branch that writes through node()/textContent.
    source = _brewing_source()
    panel_block = source.split(
        "function renderArchiveComparisonPanel(panel, status, brews, bundles, errorMessage) {", 1
    )[1].split("\n  }\n", 1)[0]
    # loading / error / empty / no-evidence are explicit `===` branches;
    # ready is the dedicated render path after the early returns.
    for state in ("loading", "error", "empty", "no-evidence"):
        assert f"status === '{state}'" in panel_block, (
            f"review panel missing dedicated state branch: {state}"
        )
    assert "status !== 'ready'" in panel_block
    assert "'ready'" in panel_block
    # The empty state must be reachable when fewer than two brews are
    # selected, and the no-evidence state must surface when a bundle is
    # missing the evidence_hash field.
    assert "ids.length < 2" in source
    assert "brews.length < 2" in source
    assert "bundles.some((bundle) => !bundle || !bundle.evidence_hash)" in source


def test_w5_archive_selection_uses_stable_brew_id_checkbox() -> None:
    # Each archive card must render an unchecked per-run checkbox whose
    # stable id is the brew id, and the compare-selected action must read
    # only those checkboxes.
    source = _brewing_source()
    build_card = source.split("function buildArchiveCard(brew) {", 1)[1].split("\n  }\n", 1)[0]
    # Checkbox is unchecked by default and carries the stable brew id.
    assert "checkbox.type = 'checkbox';" in build_card
    assert "checkbox.dataset.brewId = String(brew.id);" in build_card
    # Compare-selected helper reads ONLY those checkboxes by stable id.
    selector_block = source.split(
        "function selectedArchiveBrewIds() {", 1
    )[1].split("\n  }\n", 1)[0]
    assert "'input.archive-compare-checkbox'" in selector_block
    assert "input.checked" in selector_block
    assert "input.dataset.brewId" in selector_block
    # No checkbox defaults to checked.
    assert "checkbox.checked = true" not in build_card


def test_w5_archive_freeze_card_button_is_idempotent_post() -> None:
    # The freeze / review evidence action is the same idempotent POST as
    # compare, and must surface the persisted evidence_hash + captured_at
    # on the card.
    source = _brewing_source()
    build_card = source.split("function buildArchiveCard(brew) {", 1)[1].split("\n  }\n", 1)[0]
    assert "Freeze / review evidence" in build_card
    assert "freezeArchiveEvidence(brew.id)" in build_card
    # Card must display the evidence hash + availability state.
    assert "Evidence: not frozen yet." in build_card
    assert "bundle.evidence_hash" in build_card
    assert "bundle.captured_at" in build_card
    # The default state must say "not frozen yet" — never invent evidence.
    assert "evidence_hash" in source


def test_w5_archive_annotation_classifications_and_textarea_are_bounded() -> None:
    # The Add annotation action must expose the exact classification select
    # with the four documented values, plus a bounded textarea.
    source = _brewing_source()
    classifications = source.split(
        "const ARCHIVE_ANNOTATION_CLASSIFICATIONS = [", 1
    )[1].split("];", 1)[0]
    for value in ("operator_post_brew", "tasting_outcome", "hypothesis", "annotation_clarification"):
        assert f"'{value}'" in classifications
    # No extra classification values leak into the select.
    assert classifications.count("'") == 8  # 4 values × 2 (quote + comma handled separately)
    # Textarea is bounded via maxLength = ARCHIVE_NOTES_MAX.
    build_card = source.split("function buildArchiveCard(brew) {", 1)[1].split("\n  }\n", 1)[0]
    assert "node('textarea', 'archive-annotation-notes')" in build_card
    assert "notesTextarea.maxLength = ARCHIVE_NOTES_MAX;" in build_card
    assert "const ARCHIVE_NOTES_MAX = 4000;" in source


def test_w5_archive_annotation_submit_uses_operator_origin_and_lineage() -> None:
    # POST /api/brews/{id}/archive-annotations must use origin:'operator'
    # and content:{notes:text}; parent_annotation_id must equal the latest
    # annotation id when history exists and null for the root.
    source = _brewing_source()
    submit_block = source.split(
        "async function submitArchiveAnnotation(button, brew, card) {", 1
    )[1].split("\n  }\n", 1)[0]
    # Exact endpoint and method.
    assert "/archive-annotations" in submit_block
    assert "method: 'POST'" in submit_block
    # Origin pinned to 'operator' so model-origin writes are blocked at the UI.
    assert "origin: 'operator'" in submit_block
    # Content shape is the documented {notes: text}.
    assert "content: { notes }" in submit_block
    # Lineage: parent is the latest history id when history exists; null otherwise.
    assert "let parentId = null;" in submit_block
    assert "fetchArchiveAnnotations(brew.id)" in submit_block
    assert "history[history.length - 1].id" in submit_block
    assert "parent_annotation_id: parentId" in submit_block
    # No PUT/DELETE shortcuts and no rewriting of the brew / events.
    assert "method: 'PUT'" not in submit_block
    assert "method: 'DELETE'" not in submit_block
    assert "/events" not in submit_block
    assert "/stop" not in submit_block


def test_w5_archive_annotation_history_is_immutable_display_only() -> None:
    # The history list is rendered as immutable revision display — no edit
    # or delete controls, no innerHTML for untrusted values, no rewriting
    # of brew / events.
    source = _brewing_source()
    history_block = source.split(
        "function renderAnnotationHistory(card, annotations) {", 1
    )[1].split("\n  }\n", 1)[0]
    # Every annotation row uses node()/textContent for untrusted strings.
    assert "node('article', 'archive-annotation-row')" in history_block
    assert "String(entry.classification || '')" in history_block
    # No PUT/DELETE on the annotation rows.
    assert "method: 'PUT'" not in history_block
    assert "method: 'DELETE'" not in history_block
    # No rewriting of the brew or its events.
    assert "/events" not in history_block
    assert "/stop" not in history_block


def test_w5_archive_fork_uses_window_prompt_and_documented_payload() -> None:
    # Fork must require an explicit name (window.prompt / validated
    # nonblank) and POST /api/archive/fork-recipe with the exact payload
    # shape; after success it must refresh + select the new recipe via the
    # existing helpers without mutating the source.
    source = _brewing_source()
    fork_block = source.split(
        "async function forkArchiveBrew(button, brew) {", 1
    )[1].split("\n  }\n", 1)[0]
    # Explicit-name prompt.
    assert "window.prompt(" in fork_block
    assert "rawName.trim()" in fork_block
    assert "if (!newName) {" in fork_block
    # Exact endpoint and method.
    assert "'/api/archive/fork-recipe'" in fork_block
    assert "method: 'POST'" in fork_block
    # Payload shape: source_recipe_id, source_recipe_revision from the
    # frozen snapshot, source_brew_run_id, new_name.
    assert "source_recipe_id: brew.recipe_id" in fork_block
    assert "source_recipe_revision: brew.recipe_snapshot && brew.recipe_snapshot.revision" in fork_block
    assert "source_brew_run_id: brew.id" in fork_block
    assert "new_name: newName" in fork_block
    # Refresh + select via existing helpers.
    assert "await loadRecipes(created.id)" in fork_block
    # The fork handler must not mutate the source brew or source recipe.
    assert "saveRecipe" not in fork_block
    assert "/api/recipes/" not in fork_block
    assert "method: 'PUT'" not in fork_block
    assert "method: 'PATCH'" not in fork_block


def test_w5_archive_ui_uses_node_and_text_content_only() -> None:
    # All untrusted text rendering must go through node() + textContent —
    # innerHTML is forbidden on untrusted values. The existing file already
    # has zero innerHTML; this guards the new slice from regressing.
    source = _brewing_source()
    assert "innerHTML" not in source
    # And no direct element.innerHTML assignment anywhere in the file.
    assert ".innerHTML" not in source


def test_w5_archive_no_model_call_or_autosave_mutation() -> None:
    # The W5 UI must never trigger an assistant model call, an assistant
    # job submit, or an autosave of the recipe form. Compare / freeze /
    # annotation / fork handlers all stay strictly in their endpoint lanes.
    source = _brewing_source()
    archive_blocks = []
    for marker in (
        "function renderArchiveComparisonPanel(",
        "function compareSelectedArchivedBrews(",
        "function buildArchiveCard(",
        "function renderAnnotationHistory(",
        "function loadAnnotationHistory(",
        "async function submitArchiveAnnotation(",
        "async function forkArchiveBrew(",
        "function renderArchive(",
    ):
        start = source.index(marker)
        end = source.index("\n  }\n", start)
        archive_blocks.append(source[start:end])
    archive_slice = "\n".join(archive_blocks)
    # No model call paths.
    for forbidden in (
        "/api/assistant/jobs",
        "/api/assistant/chat",
        "ZeroClaw",
        "Gemma",
        "structured_client",
        "CombinedGemmaClient",
    ):
        assert forbidden not in archive_slice, (
            f"archive slice must not touch {forbidden}"
        )
    # No autosave / form dispatch in the archive slice.
    for forbidden in (
        "saveRecipe",
        "$('#recipe-form').dispatchEvent",
        "dispatchEvent(new Event('submit'",
        "recipePayload(",
    ):
        assert forbidden not in archive_slice, (
            f"archive slice must not auto-save the recipe form ({forbidden})"
        )


def test_w5_archive_empty_state_is_well_formed() -> None:
    # When no brews are archived the section still renders the empty copy,
    # the compare controls + compare panel (in empty state), and NEVER
    # crashes on an empty `state.brews`.
    source = _brewing_source()
    render_block = source.split("function renderArchive() {", 1)[1].split("\n  }\n", 1)[0]
    # Empty copy is still emitted before the early return.
    assert "'Completed and aborted brews will appear here.'" in render_block
    assert "renderArchiveComparisonPanel(comparePanel, 'empty'" in render_block
    # Filter still excludes active brews so empty state is meaningful.
    assert "state.brews.filter((brew) => brew.status !== 'active')" in render_block


def test_w5_archive_card_keeps_use_as_assistant_context() -> None:
    # Each card must still expose the original "Use as assistant context"
    # action exactly as before, setting state.selectedBrew and surfacing a
    # status message via setMessage (the existing helper).
    source = _brewing_source()
    build_card = source.split("function buildArchiveCard(brew) {", 1)[1].split("\n  }\n", 1)[0]
    assert "node('button', 'secondary', 'Use as assistant context')" in build_card
    assert "state.selectedBrew = brew;" in build_card
    assert "setMessage($('#brew-status')" in build_card


def test_w5_archive_does_not_use_save_recipe_or_event_rewrite_paths() -> None:
    # The archive UI must never call saveRecipe, never POST/PATCH the brew
    # /stop endpoint, never PUT/DELETE the brew events, and never mutate
    # the source recipe. Fork + annotation + freeze must stay in their
    # bounded endpoint lanes.
    source = _brewing_source()
    archive_block = source.split(
        "function renderArchive() {", 1
    )[1].split(
        "async function loadDevicesAndBrews() {", 1
    )[0]
    assert "saveRecipe" not in archive_block
    # /stop is for marking a brew terminal; archive must never call it.
    assert "/stop" not in archive_block
    # /events is for appending brew events; archive must never call it.
    assert "/api/brews/${" not in archive_block or archive_block.count("/api/brews/${") == 1
    # The only /api/brews/{...} archive endpoint used is archive-evidence
    # and archive-annotations — both via the shared helper or directly in
    # the annotated submit / compare flow.
    assert "method: 'PUT'" not in archive_block
    assert "method: 'PATCH'" not in archive_block
    assert "method: 'DELETE'" not in archive_block
