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


def test_watch_page_shows_ai_picked_categories(client):
    from deal_finder import site_categories
    from deal_finder.db import session_scope
    from deal_finder.models import Watch

    wid = client.post(
        "/api/watches",
        json={"name": "Mac Mini", "marketplaces": ["tutti", "ricardo", "facebook"],
              "search_params": {"make": "Mac", "model": "Mini"}},
    ).json()["id"]
    page = client.get(f"/watches/{wid}").text
    assert "not picked yet" in page  # AI disabled in tests -> nothing picked

    with session_scope() as s:  # what a background pick stores
        w = s.get(Watch, wid)
        text = site_categories.describe_watch(w)
        basis = {a.key: site_categories._site_basis(text, a) for a in site_categories.category_adapters(w)}
        w.site_categories = {"sites": {
            "tutti": {"basis": basis["tutti"], "id": "computers", "path": "computers accessories > computers",
                      "candidates": [
                          {"id": "computers", "path": "computers accessories > computers"},
                          {"id": "tablets", "path": "computers accessories > tablets"},
                      ]},
            "ricardo": {"basis": basis["ricardo"], "id": None, "path": None,
                        "candidates": [{"id": "39289", "path": "Computer > PC"}]},
        }}
    page = client.get(f"/watches/{wid}").text
    assert "tutti.ch: <strong>computers accessories &gt; computers</strong> (AI)" in page
    assert "Ricardo.ch: <strong>all categories</strong> (AI)" in page
    assert "not picked yet" not in page
    assert "Facebook" not in page.split("Searched category")[1].split("</p>")[0]  # no category support

    # The edit form offers the candidates; picking a runner-up there is what gets searched.
    form = client.get(f"/watches/{wid}/edit").text
    assert 'name="sitecat_tutti"' in form and 'value="tablets"' in form
    assert 'name="sitecat_ricardo"' in form and 'value="39289"' in form
    edit = {"name": "Mac Mini", "category": "car", "marketplaces": ["tutti", "ricardo", "facebook"],
            "sp_make": "Mac", "sp_model": "Mini", "sitecat_tutti": "tablets", "sitecat_ricardo": "39289"}
    assert client.post(f"/watches/{wid}", data=edit, follow_redirects=False).status_code == 303
    page = client.get(f"/watches/{wid}").text
    assert "tutti.ch: <strong>computers accessories &gt; tablets</strong> (your choice)" in page
    assert "Ricardo.ch: <strong>Computer &gt; PC</strong> (your choice)" in page


def test_watch_form_has_category_field(client):
    page = client.get("/watches/new").text
    assert 'name="sp_item_category"' in page


def _tag(page: str, marker: str) -> str:
    """The opening tag containing ``marker``."""
    i = page.index(marker)
    return page[page.rindex("<", 0, i): page.index(">", i) + 1]


def test_category_ui_hidden_server_side_without_tutti_or_ricardo(client):
    wid = client.post("/api/watches", json={"name": "FB only", "marketplaces": ["facebook"]}).json()["id"]
    page = client.get(f"/watches/{wid}/edit").text
    assert " hidden" in _tag(page, 'id="site-categories"')            # whole section
    assert " hidden" in _tag(page, 'data-site-category="tutti"')      # each dropdown
    assert " hidden" in _tag(page, 'data-site-category="ricardo"')
    assert " hidden" in page[page.rindex("<label", 0, page.index('name="sp_item_category"')):page.index('name="sp_item_category"')]

    wid = client.post("/api/watches", json={"name": "tutti", "marketplaces": ["tutti", "facebook"]}).json()["id"]
    page = client.get(f"/watches/{wid}/edit").text
    assert " hidden" not in _tag(page, 'id="site-categories"')
    assert " hidden" not in _tag(page, 'data-site-category="tutti"')
    assert " hidden" in _tag(page, 'data-site-category="ricardo"')


def _browser_submission(page: str) -> list[tuple[str, str]]:
    """The (name, value) pairs a browser would submit for the page's form: checked boxes
    only, a select's selected option, textarea text, and nothing that's disabled."""
    from html.parser import HTMLParser

    class Form(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.data, self.select, self.textarea, self.first_option = [], None, None, None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "input" and a.get("name") and "disabled" not in a:
                if a.get("type") in ("checkbox", "radio") and "checked" not in a:
                    return
                self.data.append((a["name"], a.get("value", "")))
            elif tag == "select":
                self.select = None if "disabled" in a or not a.get("name") else [a["name"], None]
                self.first_option = None
            elif tag == "option" and self.select is not None:
                value = a.get("value", "")
                self.first_option = self.first_option if self.first_option is not None else value
                if "selected" in a:
                    self.select[1] = value
            elif tag == "textarea" and a.get("name"):
                self.textarea = [a["name"], ""]

        def handle_data(self, data):
            if self.textarea is not None:
                self.textarea[1] += data

        def handle_endtag(self, tag):
            if tag == "select" and self.select is not None:
                self.data.append((self.select[0], self.select[1] if self.select[1] is not None else self.first_option))
                self.select = None
            elif tag == "textarea" and self.textarea is not None:
                self.data.append((self.textarea[0], self.textarea[1].replace("\n", "\r\n")))  # browsers send CRLF
                self.textarea = None

    form = Form()
    form.feed(page[page.index("<form"):page.index("</form>")])
    return form.data


def _post_form(client, url: str, pairs: list[tuple[str, str]]):
    """POST like a browser: urlencoded, repeated keys (marketplaces) kept."""
    from urllib.parse import urlencode

    return client.post(url, content=urlencode(pairs), follow_redirects=False,
                       headers={"content-type": "application/x-www-form-urlencoded"})


def test_saving_the_form_unchanged_doesnt_rerun_the_ai(client, monkeypatch):
    """Rerun the AI category pick only when the form changed: re-submitting the edit form
    exactly as rendered must keep the stored pick current and start no AI pick."""
    from deal_finder import site_categories
    from deal_finder.config import Settings
    from deal_finder.db import session_scope
    from deal_finder.models import Watch

    class Stub:
        def __init__(self, answers):
            self.answers = list(answers)

        def chat(self, messages, **kw):
            from deal_finder.ai.client import AiUnavailable

            if not self.answers:
                raise AiUnavailable("no more answers")
            return self.answers.pop(0)

    # Created like a user would: through the new-watch form.
    new = _browser_submission(client.get("/watches/new?watch_type=general").text)
    new = [(k, v) for k, v in new if k not in ("name", "sp_query", "marketplaces")]
    new += [("name", "Mac Mini"), ("sp_query", "Mac Mini M2"), ("marketplaces", "tutti"), ("marketplaces", "ricardo")]
    r = _post_form(client, "/watches", new)
    wid = int(r.headers["location"].rsplit("/", 1)[1])
    with session_scope() as s:  # the AI's pick, as the background thread would store it
        w = s.get(Watch, wid)
        site_categories.resolve(s, w, Settings(ai_enabled=True), ai_client=Stub(["1", "1", "1", "1"] * 4))
        assert site_categories.is_current(w)
        stored = dict(w.site_categories)

    started = []
    monkeypatch.setattr(site_categories, "runtime_settings", lambda s: Settings(ai_enabled=True))
    monkeypatch.setattr(site_categories.threading, "Thread",
                        lambda target, name, daemon: type("T", (), {"start": lambda self: started.append(name)})())

    unchanged = _browser_submission(client.get(f"/watches/{wid}/edit").text)
    assert ("sitecat_tutti", stored["sites"]["tutti"]["id"] or "") in unchanged  # dropdown is submitted too
    assert _post_form(client, f"/watches/{wid}", unchanged).status_code == 303
    with session_scope() as s:
        w = s.get(Watch, wid)
        assert site_categories.is_current(w)
        assert w.site_categories == stored
    assert started == []  # no AI pick started

    # Whereas a real change to what the AI sees does start one.
    changed = [(k, "Mac Studio" if k == "sp_query" else v) for k, v in unchanged]
    _post_form(client, f"/watches/{wid}", changed)
    assert started == [f"site-categories-{wid}"]
