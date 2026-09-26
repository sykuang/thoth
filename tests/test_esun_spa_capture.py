from types import SimpleNamespace as NS
import json
import pytest


def load():
    from backend.banks.esun_spa import capture

    return capture


class Session:
    def __init__(self, page):
        self.page, self.handlers, self.bodies, self.commands = page, {}, {}, []

    def on(self, name, fn):
        self.handlers[name] = fn

    def remove_listener(self, name, fn):
        self.handlers.pop(name, None)

    def detach(self):
        self.detached = True

    def send(self, name, args=None):
        self.commands.append(name)
        if name == "Page.getFrameTree":
            return {
                "frameTree": {
                    "frame": {
                        "id": "main",
                        "loaderId": getattr(self.page, "loader_id", "doc"),
                        "url": self.page.main_frame.url,
                    }
                }
            }
        if name == "Network.getResponseBody":
            return {"body": self.bodies[args["requestId"]]}
        assert name == "Network.enable"


class Page:
    def __init__(self):
        self.main_frame = NS(url="https://ebank.esunbank.com.tw/home", page=self)
        self.frames, self.handlers, self.sessions = [self.main_frame], {}, []
        self.context = NS(new_cdp_session=self.new_session)

    def new_session(self, page):
        s = Session(self)
        self.sessions.append(s)
        return s

    def on(self, name, fn):
        self.handlers[name] = fn

    def remove_listener(self, name, fn):
        self.handlers.pop(name, None)

    def wait_for_timeout(self, ms):
        raise AssertionError("unexpected wait")


class Response:
    def __init__(self, req, raw):
        self.request, self.url, self.status = req, req.url, 200
        self.headers = {"content-type": "application/json"}

    def json(self):
        raise AssertionError("Response.json forbidden")

    def body(self):
        raise AssertionError("Response.body forbidden")


def issue(m, c, p, path=None, raw=None, finish=True, **changes):
    path = path or m.PATHS[0]
    req = NS(
        url=m.ORIGIN + path,
        method="POST",
        frame=p.main_frame,
        post_data="{}",
        headers={"content-type": "application/json", "authorization": "Bearer NEVER"},
        redirected_from=None,
        redirected_to=None,
    )
    req.__dict__.update(changes)
    c._on_request(req)
    if not finish:
        return req
    return deliver(m, c, p, req, path, raw)


def deliver(m, c, p, req, path, raw=None):
    raw = raw if raw is not None else json.dumps({"resultCode": "0000", "resultBody": {"ok": True}})
    resp = Response(req, raw)
    observer = c.observers[path]
    rid = str(c._requests[id(req)])
    observer._request(
        {
            "requestId": rid,
            "request": {"url": req.url, "method": req.method, "postData": req.post_data},
            "frameId": "main",
            "loaderId": "doc",
            "documentURL": p.main_frame.url,
        }
    )
    observer._event(
        "responseReceived",
        {"requestId": rid, "frameId": "main", "loaderId": "doc", "response": {"url": req.url, "status": 200}},
    )
    observer._event("dataReceived", {"requestId": rid, "dataLength": len(raw.encode())})
    observer._event("loadingFinished", {"requestId": rid})
    observer.session.bodies[rid] = raw
    c._on_response(resp)
    return req


def test_passive_native_success():
    m = load()
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    b = c.snapshot()
    issue(m, c, p)
    assert m.require_current_success(c, b, m.PATHS[0]) == ({"ok": True}, {})
    assert c.auth_token == "" and c.auth_token_events == []
    c.detach(p)
    c.detach(p)
    assert not p.handlers and all(s.detached for s in p.sessions)


@pytest.mark.parametrize("mode", ["pending", "failed", "reordered", "duplicate"])
def test_newer_or_duplicate_invalidates(mode):
    m = load()
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    b = c.snapshot()
    old = issue(m, c, p)
    if mode == "duplicate":
        c._on_response(Response(old, ""))
    else:
        new = issue(m, c, p, finish=False)
        if mode == "failed":
            c._on_request_failed(new)
        if mode == "reordered":
            c._on_response(Response(old, ""))
    with pytest.raises(ValueError):
        m.require_current_success(c, b, m.PATHS[0])


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '{"resultCode":0,"resultBody":{}}',
        '{"resultCode":"0000","resultBody":[]}',
        '{"resultCode":"bad","resultBody":{"secret":"NEVER"}}',
        '{"resultCode":"0000","resultCode":"0000","resultBody":{}}',
        '{"resultCode":"0000","resultBody":{"n":NaN}}',
        "x" * (2 * 1024 * 1024 + 1),
    ],
)
def test_bad_response_retains_no_body(raw):
    m = load()
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    b = c.snapshot()
    issue(m, c, p, raw=raw)
    assert not c.hits and c.rejected == 1
    with pytest.raises(ValueError):
        m.require_current_success(c, b, m.PATHS[0])


@pytest.mark.parametrize(
    "change",
    [
        {"method": "GET"},
        {"post_data": "x" * 16385},
        {"post_data": "[]"},
        {"headers": {"content-type": "application/jsonp"}},
        {"redirected_from": object()},
    ],
)
def test_request_guards(change):
    m = load()
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    issue(m, c, p, **change)
    assert c.issued_count(m.PATHS[0]) == 1 and not c.hits
    assert "Network.getResponseBody" not in p.sessions[0].commands


def test_foreign_frame_and_other_path_not_captured():
    m = load()
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    issue(m, c, p, frame=NS(url=p.main_frame.url, page=p))
    assert not c.hits
    before = c.request_sequence
    for url in (
        m.ORIGIN + "/login",
        m.ORIGIN + m.PATHS[0] + "?token=NEVER",
        "http://ebank.esunbank.com.tw" + m.PATHS[0],
    ):
        c._on_request(NS(url=url, method="POST", frame=p.main_frame))
        for observer in c.observers.values():
            observer._request({"requestId": "foreign", "request": {"url": url}})
    assert c.request_sequence == before + 1  # Disallowed same-origin query invalidates.
    assert all("foreign" not in o.records for o in c.observers.values())
    assert not c.auth_token_events and not c.auth_token


def test_combined_query_budget_denies_request_after_cap(monkeypatch):
    m = load()
    monkeypatch.setattr(m.SpaCollector, "MAX_QUERY_REQUESTS", 2)
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    issue(m, c, p, path=m.PATHS[2], finish=False)
    issue(m, c, p, path=m.PATHS[5], finish=False)
    third = issue(m, c, p, path=m.PATHS[2], finish=False)
    assert c.query_issued_count() == 3 and id(third) not in c.owned
    c.detach(p)


def test_multiple_large_pages_share_total_not_per_response_budget():
    m = load()
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    raw = json.dumps({"resultCode": "0000", "resultBody": {"padding": "x" * 1_050_000}})
    for index in range(2):
        baseline = c.snapshot()
        issue(m, c, p, path=m.PATHS[2], raw=raw, post_data=json.dumps({"page": index}))
        assert m.require_current_success(c, baseline, m.PATHS[2])[0]["padding"] == "x" * 1_050_000
    assert c.admitted_bytes == 2 * len(raw.encode())
    assert not c.observers[m.PATHS[2]].bad
    c.detach(p)


@pytest.mark.parametrize("limit_kind", ["single", "total"])
def test_spa_observer_limits_still_reject_before_body_read(monkeypatch, limit_kind):
    m = load()
    monkeypatch.setattr(m, "LIMIT", 100)
    monkeypatch.setattr(m, "TOTAL", 150)
    p, c = Page(), m.SpaCollector()
    c.attach(p)
    raw = json.dumps({"resultCode": "0000", "resultBody": {"padding": "x" * (55 if limit_kind == "single" else 30)}})
    if limit_kind == "total":
        issue(m, c, p, path=m.PATHS[2], raw=raw, post_data='{"page":1}')
        assert c.hits
    observer = c.observers[m.PATHS[2]]
    before = observer.session.commands.count("Network.getResponseBody")
    baseline = c.snapshot()
    issue(m, c, p, path=m.PATHS[2], raw=raw, post_data='{"page":2}')
    with pytest.raises(ValueError):
        m.require_current_success(c, baseline, m.PATHS[2])
    assert observer.session.commands.count("Network.getResponseBody") == before
    c.detach(p)


def test_limits_and_partial_attach_cleanup():
    m = load()
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    assert all(o.LIMIT == 2 * 1024 * 1024 and o.MAX_RECORDS == c.MAX_QUERY_REQUESTS for o in c.observers.values())
    for i in range(9):
        issue(m, c, p, finish=False)
    assert c.issued_count(m.PATHS[0]) == 9
    for _ in range(m.TOTAL // m.LIMIT):
        assert c._reserve(m.LIMIT)
    assert not c._reserve(1)
    c.detach()
    p = Page()
    c = m.SpaCollector()
    original = p.context.new_cdp_session

    def fail(page):
        if p.sessions:
            raise RuntimeError("primary attach failure")
        return original(page)

    p.context.new_cdp_session = fail
    with pytest.raises(RuntimeError, match="primary attach failure"):
        c.attach(p)
    assert p.sessions[0].detached and not p.handlers and c.page is None


def test_late_cdp_failure_invalidates_success():
    m = load()
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    b = c.snapshot()
    issue(m, c, p)
    c.observers[m.PATHS[0]]._event("loadingFailed", {"requestId": "1"})
    with pytest.raises(ValueError):
        m.require_current_success(c, b, m.PATHS[0])


@pytest.mark.parametrize("path_index", [0, 1, 2])
@pytest.mark.parametrize("newer_failed", [False, True])
@pytest.mark.parametrize(
    "marker",
    [
        "google",
        "gtm",
        "omtrdc",
        "doubleclick",
        "analytics",
        "datalayer",
        "celebrus",
        "faro",
        "/assets/",
        "/locales/",
        ".js",
        ".css",
        ".ico",
        ".png",
        ".jpg",
        ".svg",
        ".woff",
        ".woff2",
        ".gif",
    ],
)
def test_filtered_newer_issuance_blocks_delayed_old_response(path_index, newer_failed, marker):
    m = load()
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    b = c.snapshot()
    path = m.PATHS[path_index]
    old = issue(m, c, p, path=path, finish=False)
    newer = NS(url=m.ORIGIN + path + "?marker=" + marker, method="POST", frame=p.main_frame, headers={})
    assert m.ResponseCollector.SKIP_RE.search(newer.url) is not None
    c._on_request(newer)
    if newer_failed:
        c._on_request_failed(newer)
    deliver(m, c, p, old, path)
    with pytest.raises(ValueError):
        m.require_current_success(c, b, path)
    assert c.issued_count(path) == 2 and c.request_sequence == 2
    assert not c.hits
    assert "Network.getResponseBody" not in p.sessions[path_index].commands


@pytest.mark.parametrize("path_index", [0, 1, 2])
def test_frame_metadata_failure_still_invalidates_pending_success(path_index):
    m = load()
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    b = c.snapshot()
    path = m.PATHS[path_index]
    old = issue(m, c, p, path=path, finish=False)
    c._on_request(NS(url=m.ORIGIN + path))  # Native metadata unavailable: no capture.
    deliver(m, c, p, old, path)
    assert c.issued_count(path) == 2 and c.request_sequence == 2 and not c.hits
    with pytest.raises(ValueError):
        m.require_current_success(c, b, path)
    assert "Network.getResponseBody" not in p.sessions[path_index].commands


def test_disallowed_query_issuance_invalidates_earlier_success():
    m = load()
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    b = c.snapshot()
    issue(m, c, p)
    c._on_request(NS(url=m.ORIGIN + m.PATHS[0] + "?unexpected=1", method="POST", frame=p.main_frame, headers={}))
    assert c.issued_count(m.PATHS[0]) == 2
    with pytest.raises(ValueError):
        m.require_current_success(c, b, m.PATHS[0])


def test_detach_clears_body_references_and_old_attachment_baseline():
    m = load()
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    b = c.snapshot()
    issue(m, c, p)
    c.detach()
    assert not c._latest_spa and not c.hits and not c.owned
    assert callable(c.latest)  # Preserve inherited collector API.
    c.attach(p)
    fresh = c.snapshot()
    issue(m, c, p)
    assert m.require_current_success(c, fresh, m.PATHS[0])[0] == {"ok": True}
    with pytest.raises(ValueError):
        m.require_current_success(c, b, m.PATHS[0])


def test_old_snapshot_without_prior_request_cannot_cross_reattach():
    m = load()
    p = Page()
    c = m.SpaCollector()
    c.attach(p)
    b = c.snapshot()
    c.detach()
    c.attach(p)
    issue(m, c, p)
    with pytest.raises(ValueError):
        m.require_current_success(c, b, m.PATHS[0])


"""Late native failure must invalidate only its accepted request identity."""


@pytest.mark.parametrize("index", range(6))
def test_late_failure_invalidates_accepted_request_after_bookkeeping_cleanup(index):
    m = load()
    page = Page()
    c = m.SpaCollector()
    c.attach(page)
    path = m.PATHS[index]
    baseline = c.snapshot()
    request = issue(m, c, page, path=path)
    assert m.require_current_success(c, baseline, path) == ({"ok": True}, {})
    assert id(request) not in c._requests
    counts = c.snapshot()["counts"]
    c._on_request_failed(request)
    assert c.snapshot()["counts"] == counts
    with pytest.raises(ValueError, match="no current owned SPA success"):
        m.require_current_success(c, baseline, path)
    assert not c.hits
    c.detach()


@pytest.mark.parametrize("index", range(6))
@pytest.mark.parametrize("old_completed", [False, True])
def test_old_failure_does_not_erase_newer_success(index, old_completed):
    m = load()
    page = Page()
    c = m.SpaCollector()
    c.attach(page)
    path = m.PATHS[index]
    old = issue(m, c, page, path=path, finish=old_completed)
    baseline = c.snapshot()
    issue(m, c, page, path=path)
    c._on_request_failed(old)
    assert m.require_current_success(c, baseline, path) == ({"ok": True}, {})
    assert len(c.hits) == 1
    c.detach()


@pytest.mark.parametrize("index", range(6))
def test_same_url_foreign_request_does_not_invalidate_accepted_identity(index):
    m = load()
    page = Page()
    c = m.SpaCollector()
    c.attach(page)
    path = m.PATHS[index]
    baseline = c.snapshot()
    accepted = issue(m, c, page, path=path)
    foreign = NS(**accepted.__dict__)
    c._on_request_failed(foreign)
    assert m.require_current_success(c, baseline, path) == ({"ok": True}, {})
    c.detach()
