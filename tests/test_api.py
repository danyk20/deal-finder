from __future__ import annotations


def test_health(client):
    body = client.get("/api/health").json()
    assert body["db"] is True
    assert "scheduler_running" in body


def test_categories_and_marketplaces(client):
    cats = client.get("/api/categories").json()
    assert any(c["key"] == "car" for c in cats)
    car = next(c for c in cats if c["key"] == "car")
    assert any(f["name"] == "model" for f in car["search_param_fields"])
    assert car["default_questions"]

    keys = {m["key"] for m in client.get("/api/marketplaces").json()}
    assert {"tutti", "ricardo", "autoscout24", "facebook"} <= keys
    assert "demo" not in keys  # internal/dev-only: hidden from the public listing


def _payload(**over):
    p = {
        "name": "Tesla MS", "category": "car", "marketplaces": ["demo"],
        "search_params": {"make": "Tesla", "model": "Model S"},
        "filters": {"price_max": 60000, "year_min": 2016},
        "notify_email": "me@example.com", "questions": ["Condition?"],
        "schedule_kind": "interval", "schedule_value": "1d",
    }
    p.update(over)
    return p


def test_watch_crud_run_start_stop(client, monkeypatch):
    r = client.post("/api/watches", json=_payload())
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    assert r.json()["questions"] == ["Condition?"]
    assert r.json()["notify_channel"] == "telegram"  # default channel

    assert any(w["id"] == wid for w in client.get("/api/watches").json())

    # Round-trip notify_channel/telegram_chat_id.
    r = client.patch(f"/api/watches/{wid}", json={"notify_channel": "email", "telegram_chat_id": "999"})
    assert r.json()["notify_channel"] == "email"
    assert r.json()["telegram_chat_id"] == "999"

    # Preview run (no email, no writes) finds the matching demo listings.
    res = client.post(f"/api/watches/{wid}/run-now").json()
    assert res["matched"] >= 1
    assert client.get(f"/api/watches/{wid}/matches").json() == []  # preview wrote nothing

    assert client.post(f"/api/watches/{wid}/start").json()["active"] is True
    assert client.post(f"/api/watches/{wid}/stop").json()["active"] is False

    # Dry run: opens matches in a browser tab instead of emailing, writes nothing.
    from deal_finder import pipeline

    opened = []
    monkeypatch.setattr(pipeline, "open_listings", lambda urls, **k: opened.extend(urls) or len(urls))
    res = client.post(f"/api/watches/{wid}/run-now?dry_run=true").json()
    assert res["dry_run"] is True and res["opened"] == len(opened) > 0
    assert res["emailed"] is False
    assert client.get(f"/api/watches/{wid}/matches").json() == []  # still no DB writes

    # Invalid schedule is rejected.
    bad = client.patch(f"/api/watches/{wid}", json={"schedule_value": "nonsense"})
    assert bad.status_code == 400

    assert client.delete(f"/api/watches/{wid}").status_code == 204
    assert client.get(f"/api/watches/{wid}").status_code == 404


def test_create_applies_default_questions(client):
    r = client.post("/api/watches", json=_payload(questions=[]))
    assert r.status_code == 201
    assert len(r.json()["questions"]) > 0  # defaulted from the car category


def test_settings_update(client):
    r = client.patch("/api/settings", json={"values": {"smtp_host": "smtp.test"}})
    assert r.status_code == 200
    assert r.json()["effective"]["smtp_host"] == "smtp.test"
    # Unknown key rejected.
    assert client.patch("/api/settings", json={"values": {"nope": "x"}}).status_code == 400
