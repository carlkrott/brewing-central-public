"""Phase 4 deterministic fitting and immutable calibration API contracts."""
from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor

import pytest


def _device(client, device_id="cal-device"):
    response = client.post(
        "/api/ingest",
        json={"ID": device_id, "angle": 20.0, "gravity": 1.01, "temperature": 20.0},
    )
    assert response.status_code == 200


def _linear(label="Linear", activate=True):
    return {
        "label": label,
        "activate": activate,
        "points": [{"angle": 10.0, "value": 1.0}, {"angle": 20.0, "value": 1.1}],
    }


def _quadratic(label="Quadratic", activate=True):
    return {
        "label": label,
        "order": 2,
        "activate": activate,
        "points": [
            {"angle": 0.0, "value": 1.0},
            {"angle": 1.0, "value": 1.03},
            {"angle": 2.0, "value": 1.08},
            {"angle": 3.0, "value": 1.15},
        ],
    }


def _cubic(label="Cubic", activate=False):
    return {
        "label": label,
        "order": 3,
        "activate": activate,
        "points": [
            {"angle": 0.0, "value": 1.0},
            {"angle": 1.0, "value": 1.0111},
            {"angle": 2.0, "value": 1.0248},
            {"angle": 3.0, "value": 1.0417},
            {"angle": 4.0, "value": 1.0624},
        ],
    }


def test_linear_fit_exact_bytes(app_module):
    coeffs, r2, points = app_module._fit_calibration([(0.0, 1.0), (1.0, 1.02), (2.0, 1.04)], 1)
    assert coeffs == pytest.approx([1.0, 0.02], rel=1e-12, abs=1e-12)
    assert r2 == pytest.approx(1.0)
    assert points == [(0.0, 1.0), (1.0, 1.02), (2.0, 1.04)]


def test_quadratic_fit_known_coefficients_and_r2(app_module):
    points = [(0.0, 1.0), (1.0, 1.03), (2.0, 1.08), (3.0, 1.15)]
    coeffs, r2, _ = app_module._fit_calibration(points, 2)
    assert coeffs == pytest.approx([1.0, 0.02, 0.01], rel=1e-10, abs=1e-10)
    assert r2 == pytest.approx(1.0)
    assert app_module._evaluate_polynomial(coeffs, 3.0) == pytest.approx(1.15)


def test_cubic_fit_known_coefficients_is_monotonic(app_module):
    points = [(row["angle"], row["value"]) for row in _cubic()["points"]]
    coeffs, r2, _ = app_module._fit_calibration(points, 3)
    assert coeffs == pytest.approx(
        [1.0, 0.01, 0.001, 0.0001], rel=1e-9, abs=1e-12
    )
    assert r2 == pytest.approx(1.0)
    assert app_module._evaluate_polynomial(coeffs, 2.5) == pytest.approx(1.0328125)


def test_cubic_api_persists_inactive_monotonic_fit(test_client):
    _device(test_client)
    response = test_client.post(
        "/api/device/cal-device/calibration", json=_cubic("water-series")
    )
    assert response.status_code == 200
    body = response.json()
    assert body["order"] == 3
    assert body["monotonic"] is True
    assert body["is_active"] is False
    assert len(body["coefficients"]) == 4


def test_fit_rejects_decreasing_reference_points(test_client):
    _device(test_client)
    response = test_client.post(
        "/api/device/cal-device/calibration",
        json={
            "label": "decreasing",
            "order": 2,
            "activate": False,
            "points": [
                {"angle": 10.0, "value": 1.00},
                {"angle": 20.0, "value": 1.05},
                {"angle": 30.0, "value": 1.04},
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "calibration values must be monotonic non-decreasing"
    )


def test_fit_rejects_cubic_that_reverses_between_points(test_client):
    _device(test_client)
    response = test_client.post(
        "/api/device/cal-device/calibration",
        json={
            "label": "overshoot",
            "order": 3,
            "activate": False,
            "points": [
                {"angle": 0.0, "value": 1.00},
                {"angle": 1.0, "value": 1.01},
                {"angle": 2.0, "value": 1.20},
                {"angle": 3.0, "value": 1.21},
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "fitted calibration is not monotonic over its angle range"
    )


def test_quadratic_fit_exact_bytes(app_module):
    points = [(0.0, 1.0), (1.0, 1.03), (2.0, 1.08), (3.0, 1.15)]
    coefficients, fit_r2, ordered = app_module._fit_calibration(points, 2)
    assert coefficients == [
        0.9999999999999999,
        0.02000000000000052,
        0.009999999999999815,
    ]
    assert fit_r2 == 1.0
    assert json.dumps(coefficients, separators=(",", ":"), allow_nan=False) == (
        "[0.9999999999999999,0.02000000000000052,0.009999999999999815]"
    )
    assert json.dumps(ordered, separators=(",", ":"), allow_nan=False) == (
        "[[0.0,1.0],[1.0,1.03],[2.0,1.08],[3.0,1.15]]"
    )


def test_fit_relative_spacing_boundary(app_module):
    with pytest.raises(ValueError, match="degenerate"):
        app_module._fit_calibration([(100.0, 1.0), (100.00000000005, 1.1)], 1)
    coefficients, fit_r2, _ = app_module._fit_calibration(
        [(1.0, 1.0), (1.000000000002, 1.1)], 1
    )
    assert json.dumps(coefficients, separators=(",", ":"), allow_nan=False) == (
        "[-50001106109.475136,50001106110.475136]"
    )
    assert fit_r2 == 0.9999999995343387


def test_fit_is_byte_deterministic_for_reordered_points(app_module):
    points = [(3.0, 1.15), (0.0, 1.0), (2.0, 1.08), (1.0, 1.03)]
    first = app_module._fit_calibration(points, 2)
    second = app_module._fit_calibration(list(reversed(points)), 2)
    assert first == second


def test_fit_normalizes_signed_zero(app_module):
    coeffs, r2, _ = app_module._fit_calibration([(0.0, 1.02), (1.0, 1.02)], 1)
    assert coeffs == pytest.approx([1.02, 0.0], abs=1e-12)
    assert r2 == 1.0


def test_fit_rejects_duplicate_angles(test_client):
    _device(test_client)
    response = test_client.post("/api/device/cal-device/calibration", json={
        "label": "duplicate", "points": [
            {"angle": 10.0, "value": 1.0}, {"angle": 10.0, "value": 1.1}
        ]})
    assert response.status_code == 422
    assert response.json()["detail"] == "calibration angles must be distinct"


def test_fit_rejects_insufficient_points_for_order(test_client):
    _device(test_client)
    response = test_client.post("/api/device/cal-device/calibration", json={
        "label": "short", "order": 2, "points": [
            {"angle": 10.0, "value": 1.0}, {"angle": 20.0, "value": 1.1}
        ]})
    assert response.status_code == 422
    assert response.json()["detail"] == "insufficient calibration points for order"


def test_strict_order_activate_and_numeric_validation(test_client):
    _device(test_client)
    base = _linear()
    invalid = [
        {**base, "order": True},
        {**base, "order": 1.0},
        {**base, "activate": 1},
        {**base, "points": [{"angle": True, "value": 1.0}, {"angle": 20.0, "value": 1.1}]},
        {**base, "points": [{"angle": 10.0, "value": False}, {"angle": 20.0, "value": 1.1}]},
    ]
    for payload in invalid:
        response = test_client.post("/api/device/cal-device/calibration", json=payload)
        assert response.status_code == 422, payload


def test_fit_rejects_scaled_singular_matrix(test_client, monkeypatch, app_module):
    _device(test_client)
    response = test_client.post("/api/device/cal-device/calibration", json={
        "label": "near", "points": [
            {"angle": 1.0, "value": 1.0}, {"angle": 1.0 + 1e-13, "value": 1.1}
        ]})
    assert response.status_code == 422
    assert response.json()["detail"] == "calibration fit is degenerate"


def test_post_preserves_legacy_response_keys(test_client, app_module):
    _device(test_client)
    response = test_client.post("/api/device/cal-device/calibration", json=_linear())
    assert response.status_code == 200
    body = response.json()
    assert body["order"] == 1 and body["is_active"] is True
    assert body["coefficients"] == pytest.approx([0.9, 0.01])
    assert body["a"] == pytest.approx(0.01) and body["b"] == pytest.approx(0.9)
    assert body["point_count"] == 2 and body["id"] > 0
    with app_module.db() as conn:
        assert conn.execute(
            "SELECT is_default FROM calibrations WHERE id=?", (body["id"],)
        ).fetchone()[0] == 1


def test_post_appends_immutable_history_without_rewriting_prior_row(test_client, app_module):
    _device(test_client)
    first_id = test_client.post("/api/device/cal-device/calibration", json=_linear("first")).json()["id"]
    with app_module.db() as conn:
        before = tuple(conn.execute("SELECT * FROM calibrations WHERE id=?", (first_id,)).fetchone())
    assert test_client.post("/api/device/cal-device/calibration", json=_quadratic("second")).status_code == 200
    with app_module.db() as conn:
        after = tuple(conn.execute("SELECT * FROM calibrations WHERE id=?", (first_id,)).fetchone())
        assert conn.execute("SELECT COUNT(*) FROM calibrations").fetchone()[0] == 2
    assert after == before


def test_post_activate_false_preserves_pointer(test_client):
    _device(test_client)
    first = test_client.post("/api/device/cal-device/calibration", json=_linear("first")).json()
    second = test_client.post("/api/device/cal-device/calibration", json=_quadratic("inactive", False)).json()
    state = test_client.get("/api/device/cal-device/calibration").json()
    assert second["is_active"] is False
    assert state["active_calibration_id"] == first["id"]
    assert state["calibrations"][0]["id"] == first["id"]


def test_repeated_activation_preserves_activated_at_bytes(test_client, app_module):
    _device(test_client)
    first = test_client.post("/api/device/cal-device/calibration", json=_linear("first")).json()
    second = test_client.post("/api/device/cal-device/calibration", json=_quadratic("second", False)).json()
    with app_module.db() as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM calibrations ORDER BY id")]
    url = f"/api/device/cal-device/calibration/{second['id']}/active"
    assert test_client.put(url).status_code == 200
    with app_module.db() as conn:
        first_activated_at = conn.execute(
            "SELECT activated_at FROM calibration_active WHERE device_id='cal-device'"
        ).fetchone()[0]
    assert test_client.put(url).status_code == 200
    with app_module.db() as conn:
        second_activated_at = conn.execute(
            "SELECT activated_at FROM calibration_active WHERE device_id='cal-device'"
        ).fetchone()[0]
        after = [tuple(r) for r in conn.execute("SELECT * FROM calibrations ORDER BY id")]
    assert second_activated_at == first_activated_at
    assert after == before
    assert test_client.get("/api/device/cal-device/calibration").json()["active_calibration_id"] == second["id"]


def test_put_rejects_cross_device_calibration(test_client):
    _device(test_client, "device-a")
    _device(test_client, "device-b")
    cal_id = test_client.post("/api/device/device-a/calibration", json=_linear()).json()["id"]
    response = test_client.put(f"/api/device/device-b/calibration/{cal_id}/active")
    assert response.status_code == 409


def test_get_preserves_active_only_calibrations_and_adds_history(test_client):
    _device(test_client)
    active = test_client.post("/api/device/cal-device/calibration", json=_linear("active")).json()
    inactive = test_client.post("/api/device/cal-device/calibration", json=_quadratic("inactive", False)).json()
    state = test_client.get("/api/device/cal-device/calibration").json()
    assert state["has_calibration"] is True
    assert state["active_calibration_id"] == active["id"]
    assert [r["id"] for r in state["calibrations"]] == [active["id"]]
    assert [r["id"] for r in state["history"]] == [inactive["id"], active["id"]]
    assert sum(bool(r["is_active"]) for r in state["history"]) == 1


def _calibration_db_state(app_module):
    with app_module.db() as conn:
        history = [tuple(row) for row in conn.execute(
            "SELECT * FROM calibrations ORDER BY id"
        )]
        pointers = [tuple(row) for row in conn.execute(
            "SELECT * FROM calibration_active ORDER BY device_id"
        )]
    return history, pointers


@pytest.mark.parametrize("checkpoint", [
    "post_after_history_insert",
    "post_after_pointer_upsert",
    "post_before_commit",
])
def test_post_failure_rolls_back_row_and_pointer_bytes(
    test_client, app_module, monkeypatch, checkpoint
):
    _device(test_client)
    before = _calibration_db_state(app_module)

    def fail(name):
        if name == checkpoint:
            raise RuntimeError(f"injected {checkpoint}")

    monkeypatch.setattr(app_module, "_calibration_tx_checkpoint", fail)
    with pytest.raises(RuntimeError, match="injected"):
        test_client.post("/api/device/cal-device/calibration", json=_linear())
    assert _calibration_db_state(app_module) == before


@pytest.mark.parametrize("checkpoint", [
    "activate_after_pointer_upsert",
    "activate_before_commit",
])
def test_activation_failure_preserves_pointer_bytes(
    test_client, app_module, monkeypatch, checkpoint
):
    _device(test_client)
    test_client.post("/api/device/cal-device/calibration", json=_linear("active"))
    inactive = test_client.post(
        "/api/device/cal-device/calibration", json=_quadratic("inactive", False)
    ).json()
    before = _calibration_db_state(app_module)

    def fail(name):
        if name == checkpoint:
            raise RuntimeError(f"injected {checkpoint}")

    monkeypatch.setattr(app_module, "_calibration_tx_checkpoint", fail)
    with pytest.raises(RuntimeError, match="injected"):
        test_client.put(
            f"/api/device/cal-device/calibration/{inactive['id']}/active"
        )
    assert _calibration_db_state(app_module) == before


def test_post_materializes_response_before_commit(
    test_client, app_module, monkeypatch
):
    _device(test_client)
    before_post = _calibration_db_state(app_module)

    def fail_materialization(*_args, **_kwargs):
        raise RuntimeError("injected response materialization failure")

    original = app_module._calibration_dict
    monkeypatch.setattr(app_module, "_calibration_dict", fail_materialization)
    with pytest.raises(RuntimeError, match="materialization"):
        test_client.post("/api/device/cal-device/calibration", json=_linear())
    assert _calibration_db_state(app_module) == before_post

    monkeypatch.setattr(app_module, "_calibration_dict", original)
    active = test_client.post(
        "/api/device/cal-device/calibration", json=_linear("active")
    ).json()
    inactive = test_client.post(
        "/api/device/cal-device/calibration", json=_quadratic("inactive", False)
    ).json()
    assert active["id"] != inactive["id"]
    before_put = _calibration_db_state(app_module)
    monkeypatch.setattr(app_module, "_calibration_dict", fail_materialization)
    with pytest.raises(RuntimeError, match="materialization"):
        test_client.put(
            f"/api/device/cal-device/calibration/{inactive['id']}/active"
        )
    assert _calibration_db_state(app_module) == before_put


def test_concurrent_creations_serialize_without_lost_history(test_client, app_module):
    _device(test_client)

    def create(index):
        payload = app_module.CalibrationPayload.model_validate(
            _linear(f"concurrent-{index}")
        )
        return app_module.set_calibration("cal-device", payload)

    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(pool.map(create, range(16)))
    created_ids = {row["id"] for row in created}
    assert len(created_ids) == 16
    with app_module.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM calibrations").fetchone()[0] == 16
        pointer = conn.execute(
            "SELECT calibration_id FROM calibration_active WHERE device_id='cal-device'"
        ).fetchone()[0]
        assert pointer in created_ids
        before = [tuple(row) for row in conn.execute(
            "SELECT * FROM calibrations ORDER BY id"
        )]

    def activate(calibration_id):
        return app_module.activate_calibration("cal-device", calibration_id)

    activation_ids = [row["id"] for row in created]
    with ThreadPoolExecutor(max_workers=8) as pool:
        activated = list(pool.map(activate, activation_ids))
    assert {row["id"] for row in activated} == created_ids
    with app_module.db() as conn:
        after = [tuple(row) for row in conn.execute(
            "SELECT * FROM calibrations ORDER BY id"
        )]
        pointers = conn.execute(
            "SELECT calibration_id FROM calibration_active WHERE device_id='cal-device'"
        ).fetchall()
    assert after == before
    assert len(pointers) == 1
    assert pointers[0][0] in created_ids


def test_put_materializes_response_before_commit(test_client, app_module, monkeypatch):
    _device(test_client)
    test_client.post("/api/device/cal-device/calibration", json=_linear("active"))
    inactive = test_client.post(
        "/api/device/cal-device/calibration", json=_quadratic("inactive", False)
    ).json()
    before = _calibration_db_state(app_module)

    def fail_materialization(*_args, **_kwargs):
        raise RuntimeError("injected response materialization failure")

    monkeypatch.setattr(app_module, "_calibration_dict", fail_materialization)
    with pytest.raises(RuntimeError, match="materialization"):
        test_client.put(
            f"/api/device/cal-device/calibration/{inactive['id']}/active"
        )
    assert _calibration_db_state(app_module) == before


def test_label_is_validated_after_stripping(test_client):
    _device(test_client)
    assert test_client.post(
        "/api/device/cal-device/calibration", json={**_linear(), "label": "   "}
    ).status_code == 422
    saved = test_client.post(
        "/api/device/cal-device/calibration", json={**_linear(), "label": "  trimmed  "}
    )
    assert saved.status_code == 200
    assert saved.json()["label"] == "trimmed"
    assert test_client.post(
        "/api/device/cal-device/calibration",
        json={**_linear(), "label": " " + "x" * 81 + " "},
    ).status_code == 422


def test_post_unknown_device_is_404_and_atomic(test_client, app_module):
    before = _calibration_db_state(app_module)
    response = test_client.post(
        "/api/device/missing-device/calibration", json=_linear()
    )
    assert response.status_code == 404
    assert _calibration_db_state(app_module) == before


def test_put_unknown_device_missing_calibration_and_cross_device_statuses(test_client):
    assert test_client.put(
        "/api/device/missing/calibration/999/active"
    ).status_code == 404
    _device(test_client, "device-a")
    _device(test_client, "device-b")
    assert test_client.put(
        "/api/device/device-a/calibration/999/active"
    ).status_code == 404
    calibration_id = test_client.post(
        "/api/device/device-a/calibration", json=_linear()
    ).json()["id"]
    assert test_client.put(
        f"/api/device/device-b/calibration/{calibration_id}/active"
    ).status_code == 409


def test_concurrent_activations_serialize_without_lost_pointer(test_client, app_module):
    _device(test_client)
    created = [
        test_client.post(
            "/api/device/cal-device/calibration",
            json=_linear(f"activation-{index}", False),
        ).json()
        for index in range(16)
    ]
    before = _calibration_db_state(app_module)[0]
    ids = [row["id"] for row in created]
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(
            lambda calibration_id: app_module.activate_calibration(
                "cal-device", calibration_id
            ),
            ids,
        ))
    assert {row["id"] for row in responses} == set(ids)
    history, pointers = _calibration_db_state(app_module)
    assert history == before
    assert len(pointers) == 1
    assert pointers[0][1] in ids
