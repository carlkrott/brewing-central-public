"""Live-browser qualification for Brew Control & Archive."""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.requires_browser


def _recipe(name: str) -> dict:
    # Payload proven by tests/test_archive_evidence.py.
    return {
        "name": name,
        "style": "fruit wine",
        "description": "W7 archive workflow fixture.",
        "base_volume_l": 10.0,
        "notes": "Initial notes.",
        "ingredients": [
            {"name": "Blackberries", "quantity": 4.0, "unit": "kg",
             "category": "fruit", "scaling": {"mode": "linear"}},
            {"name": "Starter culture", "quantity": 0.25, "unit": "kg",
             "category": "culture", "scaling": {"mode": "fixed"}},
        ],
    }


def _seed_brew(base: str, device_id: str, recipe_name: str) -> dict:
    """Create a terminal brew, freeze evidence twice, and return its records."""
    with httpx.Client(base_url=base, timeout=5.0) as client:
        assert client.post(
            "/api/ingest",
            json={"ID": device_id, "angle": 24.0, "gravity": 1.0,
                  "temperature": 20.0},
        ).status_code == 200
        recipe = client.post("/api/recipes", json=_recipe(recipe_name))
        assert recipe.status_code == 201, recipe.text
        recipe_body = recipe.json()
        brew = client.post(
            "/api/brews",
            json={"device_id": device_id, "recipe_id": recipe_body["id"],
                  "target_volume_l": 30.0},
        )
        assert brew.status_code == 201, brew.text
        brew_body = brew.json()
        event = client.post(
            f"/api/brews/{brew_body['id']}/events",
            json={"event_type": "feeding", "source": "manual",
                  "notes": "First nutrient feed.", "data": {"sugar_g": 75}},
        )
        assert event.status_code == 201, event.text
        stopped = client.post(
            f"/api/brews/{brew_body['id']}/stop",
            json={"outcome": "completed", "notes": "OK"},
        )
        assert stopped.status_code == 200, stopped.text
        first = client.post(f"/api/brews/{brew_body['id']}/archive-evidence")
        assert first.status_code == 200, first.text
        first_body = first.json()
        second = client.post(f"/api/brews/{brew_body['id']}/archive-evidence")
        assert second.status_code == 200, second.text
        assert second.json()["id"] == first_body["id"]
        assert second.json()["evidence_hash"] == first_body["evidence_hash"]
        fetched = client.get(f"/api/brews/{brew_body['id']}")
        assert fetched.status_code == 200, fetched.text
        return {
            "brew": fetched.json(),
            "recipe": recipe_body,
            "evidence": first_body,
        }


def test_archive_workflow_renders_persisted_review_annotation_and_fork(
    browser_page, live_server: str
) -> None:
    """Use the live API to seed, then exercise the real Chromium workflow."""
    fixture_a = _seed_brew(live_server, "workflow-device-a", "Workflow recipe A")
    fixture_b = _seed_brew(live_server, "workflow-device-b", "Workflow recipe B")
    brew_a = fixture_a["brew"]
    brew_b = fixture_b["brew"]
    evidence_a = fixture_a["evidence"]
    original = {
        "snapshot": evidence_a["source_snapshot_json"],
        "events": evidence_a["source_events_json"],
        "hash": evidence_a["evidence_hash"],
        "event_count": len(brew_a["events"]),
    }

    page = browser_page
    page.goto(f"{live_server}/")
    page.locator("#tab-button-brew").click()
    page.wait_for_selector("#brew-archive .archive-item")
    cards = page.locator("#brew-archive .archive-item")
    assert cards.count() >= 2

    card_a = page.locator(
        f'#brew-archive .archive-item[data-brew-id="{brew_a["id"]}"]'
    )
    card_b = page.locator(
        f'#brew-archive .archive-item[data-brew-id="{brew_b["id"]}"]'
    )
    # The card action reads the already-frozen bundle and is itself idempotent.
    for card, expected_hash in ((card_a, evidence_a["evidence_hash"]),
                                (card_b, fixture_b["evidence"]["evidence_hash"])):
        card.get_by_role("button", name="Freeze / review evidence").click()
        page.wait_for_function(
            """([id, prefix]) => document.querySelector(
              `#brew-archive .archive-item[data-brew-id="${id}"] .archive-evidence-state`
            )?.textContent.includes(prefix)""",
            arg=[str(card.get_attribute("data-brew-id")), expected_hash[:12]],
        )
    assert evidence_a["evidence_hash"][:12] in card_a.locator(
        ".archive-evidence-state"
    ).inner_text()

    # Select both archived cards so the persisted frozen review surface loads.
    page.locator(
        f'input.archive-compare-checkbox[data-brew-id="{brew_a["id"]}"]'
    ).check()
    page.locator(
        f'input.archive-compare-checkbox[data-brew-id="{brew_b["id"]}"]'
    ).check()
    page.get_by_role("button", name="Compare selected").click()
    page.wait_for_selector("#archive-compare-panel .archive-review-item")
    review = page.locator("#archive-compare-panel")
    assert review.locator(".archive-review-item").count() == 2
    assert original["hash"] in review.inner_text()
    assert "Frozen events" in review.inner_text()
    assert "feeding" in review.inner_text()

    # Add a bounded operator annotation through the page, then reload it.
    note = "Workflow A: pleasant finish; pitch warmer next time."
    card_a.locator(".archive-annotation-classification").select_option(
        "operator_post_brew"
    )
    card_a.locator(".archive-annotation-notes").fill(note)
    card_a.get_by_role("button", name="Add annotation").click()
    page.wait_for_function(
        """([id, text]) => {
          const c = document.querySelector(`#brew-archive .archive-item[data-brew-id="${id}"]`);
          return c?.querySelector('.archive-annotation-status')?.textContent.includes('Annotation recorded')
            && c?.querySelector('.archive-annotation-history')?.textContent.includes(text);
        }""",
        arg=[str(brew_a["id"]), note],
    )
    card_a.get_by_role("button", name="Reload annotations").click()
    page.wait_for_function(
        """([id, text]) => {
          const c = document.querySelector(`#brew-archive .archive-item[data-brew-id="${id}"]`);
          return c?.querySelector('.archive-annotation-history')?.textContent.includes(text);
        }""",
        arg=[str(brew_a["id"]), note],
    )

    # Fork is prompt-driven in production; accept one deterministic bounded name.
    fork_name = "Workflow A fork draft"
    page.once("dialog", lambda dialog: dialog.accept(fork_name))
    card_a.get_by_role("button", name="Fork as new recipe").click()
    page.wait_for_function(
        "name => document.querySelector('#brew-status')?.textContent.includes(name)",
        arg="Forked brew",
    )

    with httpx.Client(base_url=live_server, timeout=5.0) as client:
        recipes = client.get("/api/recipes").json()["recipes"]
        matches = [row for row in recipes if row["name"] == fork_name]
        assert len(matches) == 1
        child_id = matches[0]["id"]
        lineage = client.get(f"/api/recipes/{child_id}/lineage").json()
        assert lineage["child_recipe_id"] == child_id
        assert lineage["source_recipe_id"] == brew_a["recipe_id"]
        assert lineage["source_brew_run_id"] == brew_a["id"]

        # Refresh and verify the new recipe, annotation, and source history remain.
        page.reload()
        page.locator("#tab-button-brew").click()
        page.wait_for_selector("#brew-archive .archive-item")
        refreshed_card = page.locator(
            f'#brew-archive .archive-item[data-brew-id="{brew_a["id"]}"]'
        )
        refreshed_card.get_by_role("button", name="Reload annotations").click()
        page.wait_for_function(
            """([id, text]) => document.querySelector(
              `#brew-archive .archive-item[data-brew-id="${id}"] .archive-annotation-history`
            )?.textContent.includes(text)""",
            arg=[str(brew_a["id"]), note],
        )
        page.locator("#tab-button-recipes").click()
        page.wait_for_selector("#recipe-list .recipe-list-item")
        assert fork_name in page.locator("#recipe-list").inner_text()
        lineage_after_reload = client.get(f"/api/recipes/{child_id}/lineage").json()
        assert lineage_after_reload["child_recipe_id"] == child_id
        assert lineage_after_reload["source_recipe_id"] == brew_a["recipe_id"]
        assert lineage_after_reload["source_brew_run_id"] == brew_a["id"]

        refrozen = client.post(
            f"/api/brews/{brew_a['id']}/archive-evidence"
        ).json()
        assert refrozen["source_snapshot_json"] == original["snapshot"]
        assert refrozen["source_events_json"] == original["events"]
        assert refrozen["evidence_hash"] == original["hash"]
        assert len(client.get(f"/api/brews/{brew_a['id']}").json()["events"]) == original["event_count"]


@pytest.mark.requires_browser
def test_archive_44px_controls_remain_usable(browser_page, live_server: str) -> None:
    """Archive controls must expose computed width/height >= 44px on a
    320x568 viewport so taps remain reachable. Desktop behaviour must be
    preserved on a wider viewport (no 44px floor is enforced globally;
    only the @media (max-width:700px) block applies). The test seeds two
    archived brews via the live API and measures every archive-card
    action control plus the cross-archive compare button.
    """
    fixture_a = _seed_brew(live_server, "44px-device-a", "44px recipe A")
    fixture_b = _seed_brew(live_server, "44px-device-b", "44px recipe B")

    page = browser_page
    page.set_viewport_size({"width": 320, "height": 568})
    page.set_default_timeout(5000)
    page.goto(f"{live_server}/")
    page.locator("#tab-button-brew").click()
    page.wait_for_selector("#brew-archive .archive-item")
    # Both seeded brews must have rendered.
    page.wait_for_selector(
        f'#brew-archive .archive-item[data-brew-id="{fixture_b["brew"]["id"]}"]'
    )

    # Every archive-card action is referenced via the role of its rendered
    # text label. The compare-selected button lives inside the
    # archive-compare-controls row above the archive list and is also
    # referenced by the production class hook (.archive-compare-controls
    # button). We measure by class so we don't introduce an ID solely
    # for the test (per requirement #7: keep immutable coverage and don't
    # add IDs for the test).
    measure_targets = [
        (".archive-compare-controls button", "Compare selected"),
        (
            f'#brew-archive .archive-item[data-brew-id="{fixture_a["brew"]["id"]}"]',
            "Freeze / review evidence",
        ),
        (
            f'#brew-archive .archive-item[data-brew-id="{fixture_a["brew"]["id"]}"]',
            "Use as assistant context",
        ),
        (
            f'#brew-archive .archive-item[data-brew-id="{fixture_a["brew"]["id"]}"]',
            "Fork as new recipe",
        ),
        (
            f'#brew-archive .archive-item[data-brew-id="{fixture_a["brew"]["id"]}"]',
            "Add annotation",
        ),
        (
            f'#brew-archive .archive-item[data-brew-id="{fixture_a["brew"]["id"]}"]',
            "Reload annotations",
        ),
    ]
    sizes: dict[str, dict] = {}
    for selector, label in measure_targets:
        locator = (
            page.locator(selector).get_by_role("button", name=label, exact=True)
            if selector != ".archive-compare-controls button"
            else page.locator(selector, has_text=label)
        )
        handle = locator.first.element_handle()
        assert handle is not None, f"missing control {selector!r} with label {label!r}"
        rect = handle.bounding_box()
        assert rect is not None, f"{selector!r} ({label}) has no bounding box"
        sizes[selector + "::" + label] = {"width": rect["width"], "height": rect["height"]}
    for key, size in sizes.items():
        assert size["height"] >= 44, f"{key} height {size['height']} < 44"
        assert size["width"] >= 44, f"{key} width {size['width']} < 44"

    # Desktop behaviour must remain usable (no 44px floor required).
    page.set_viewport_size({"width": 1024, "height": 768})
    page.wait_for_selector("#brew-archive .archive-item")
    desktop = page.evaluate(
        """() => {
          const btn = document.querySelector('.archive-item button');
          const rect = btn.getBoundingClientRect();
          return { width: rect.width, height: rect.height };
        }"""
    )
    assert desktop["height"] >= 24, "desktop archive button height regressed"
