"""Real HTTP gzip/chunked regression; no bank, routing, or replay."""
import gzip
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest
from patchright.sync_api import sync_playwright

from backend.core import base


@pytest.mark.parametrize('bank,url', list(base._HISTORY_OBSERVER_URLS.items()))
@pytest.mark.parametrize('fault', ['missing', 'rejected', 'untracked', 'invalid_length', 'mismatch', 'valid'])
def test_declared_history_requires_observer_proof(bank, url, fault):
    from types import SimpleNamespace
    payload = b'{"value":"' + b'x' * 80 + b'"}'
    frame = SimpleNamespace(url=url)
    frame.page = SimpleNamespace(main_frame=frame)
    request = SimpleNamespace(url=url, method='POST', post_data='synthetic=1',
                              headers={}, frame=frame, redirected_from=None)
    reads = []
    def native_body():
        reads.append('native')
        return payload
    def observed(*args):
        reads.append('observer')
        return None if fault == 'rejected' else payload
    collector = base.ResponseCollector(bank)
    if fault != 'missing':
        collector._history_observer = SimpleNamespace(read=observed)
    if fault != 'untracked':
        collector._on_request(request)
    declared = 'invalid' if fault == 'invalid_length' else str(len(payload) + (fault == 'mismatch'))
    collector._on_response(SimpleNamespace(
        url=url, request=request, status=200,
        headers={'content-type': 'application/json', 'content-length': declared}, body=native_body,
    ))
    assert 'native' not in reads
    assert len(collector.hits) == 1
    assert (collector.hits[0].resp_json is not None) == (fault == 'valid')
    if fault in {'missing', 'untracked', 'invalid_length'}:
        assert reads == []
    else:
        assert reads == ['observer']


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b'<html>synthetic history</html>'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        self.server.posts += 1
        if self.server.mode == 'duplicate':
            time.sleep(.05)
        if self.server.mode == 'redirect' and self.path != '/end':
            self.send_response(307)
            self.send_header('Location', '/end')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        wire = self.server.payload if self.server.mode in {'identity', 'identity_declared'} else gzip.compress(self.server.payload)
        if self.server.mode not in {'identity', 'identity_declared'}:
            self.send_header('Content-Encoding', 'gzip')
        if self.server.mode in {'declared', 'identity_declared'}:
            self.send_header('Content-Length', str(len(wire)))
        else:
            self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        try:
            if self.server.mode in {'declared', 'identity_declared'}:
                self.wfile.write(wire)
                self.wfile.flush()
                return
            self.wfile.write(('%x\r\n' % len(wire)).encode() + wire + b'\r\n')
            self.wfile.flush()
            if self.server.mode == 'document_pending':
                self.server.release.wait(3)
            if self.server.mode == 'delayed':
                time.sleep(.25)
            if self.server.mode == 'broken':
                self.close_connection = True
            elif self.server.mode == 'deadline':
                time.sleep(1)
            else:
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.mark.parametrize('bank,path', [
    ('ubot.com.tw', '/MyBank/IBKB010102'),
    ('taishinbank.com.tw', '/TIBNetBank/svc/web1/rb0102/query'),
])
@pytest.mark.parametrize('mode', ['ok', 'delayed', 'identity', 'identity_declared', 'declared', 'broken', 'deadline', 'oversize', 'budget', 'missing', 'duplicate', 'redirect', 'sequential', 'iframe_hash', 'main_hash', 'duplicate_callback', 'iframe_duplicate'])
def test_real_gzip_collector(monkeypatch, bank, path, mode):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    server.payload = b'{"value":"' + b'x' * 70000 + b'"}'
    server.posts, server.mode = 0, mode
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = f'http://127.0.0.1:{server.server_port}'
    native_url = root + path
    host = 'www.ubot.com.tw' if bank == 'ubot.com.tw' else 'my.taishinbank.com.tw'
    # Only substitute the endpoint allowlist/origin parser, never HTTP responses.
    monkeypatch.setattr(base, '_HISTORY_OBSERVER_URLS', {bank: native_url}, raising=False)
    monkeypatch.setattr(base, 'urlparse', lambda url: urlparse(url.replace(root, 'https://' + host)))
    # Only deadline exhaustion uses a shortened budget; valid HTTP uses production timing.
    if mode == 'deadline':
        monkeypatch.setattr(base._HistoryBodyObserver, 'WAIT_SECONDS', .15)
    if mode in {'oversize', 'budget'}:
        monkeypatch.setattr(base._HistoryBodyObserver, 'LIMIT', 70000 if mode == 'oversize' else 100000)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(root + ('/#main' if mode == 'main_hash' else ''))
            frame = page.main_frame
            if mode in {'iframe_hash', 'iframe_duplicate'}:
                page.evaluate("url => {let f = document.createElement('iframe'); f.src = url; document.body.append(f);}", root + '/child#history')
                page.wait_for_function("document.querySelector('iframe').contentDocument?.readyState === 'complete'")
                frame = page.frames[1]
                assert frame.url == root + '/child#history'
                page.evaluate("url => {let f = document.createElement('iframe'); f.src = url; document.body.append(f);}", root + ('/child#history' if mode == 'iframe_duplicate' else '/child#other'))
                page.wait_for_function("document.querySelectorAll('iframe')[1].contentDocument?.readyState === 'complete'")
            collector = base.ResponseCollector(bank)
            collector.attach(page)
            observer = collector._history_observer
            commands = []
            original_send = observer.session.send
            def send(command, params=None):
                commands.append(command)
                if command == 'Network.getResponseBody':
                    record = observer.records[params['requestId']]
                    assert record['done'] and not record['bad']
                    assert observer.total <= observer.LIMIT
                    if mode == 'duplicate_callback':
                        before = len(commands)
                        assert original_read(responses[-1], frame, frame.url, observer.LIMIT, 0) is None
                        assert len(commands) == before
                assert command in {'Page.getFrameTree', 'Network.getResponseBody'}
                result = original_send(command, params)
                if command == 'Page.getFrameTree' and mode == 'iframe_hash':
                    cdp_frame = result['frameTree']['childFrames'][0]['frame']
                    assert cdp_frame['url'] == root + '/child'
                    assert cdp_frame['urlFragment'] == '#history'
                return result
            monkeypatch.setattr(observer.session, 'send', send)
            original_read, responses = observer.read, []
            def read(response, *args):
                if mode == 'sequential' and responses:
                    before = len(commands)
                    assert original_read(responses[0], *args) is None
                    assert len(commands) == before  # Same native Request cannot claim the next CDP ID.
                responses.append(response)
                return original_read(response, *args)
            monkeypatch.setattr(observer, 'read', read)
            from patchright.sync_api import Response
            monkeypatch.setattr(Response, 'body', lambda self: pytest.fail('native body read/replay path'))
            monkeypatch.setattr(Response, 'finished', lambda self: pytest.fail('unbounded finished wait'))
            if mode == 'missing':
                observer.close()
            requests = []
            page.on('request', lambda req: requests.append(req))
            frame.evaluate("arg => {for(let i=0;i<arg.n;i++) fetch(arg.url, {method:'POST', body:'synthetic=1'}).catch(()=>{});}", {'url': native_url, 'n': 2 if mode == 'duplicate' else 1})
            deadline = time.monotonic() + 3
            expected_hits = 2 if mode == 'duplicate' else 1
            while len(collector.hits) < expected_hits and time.monotonic() < deadline:
                page.wait_for_timeout(20)
            if mode in {'budget', 'sequential'}:
                page.evaluate("arg => {fetch(arg.url, {method:'POST', body:arg.body}).catch(()=>{});}", {'url': native_url, 'body': 'synthetic=1' if mode == 'sequential' else 'synthetic=2'})
                while len(collector.hits) < 2 and time.monotonic() < deadline:
                    page.wait_for_timeout(20)
                assert len(collector.hits) == 2
                assert collector.hits[1].resp_json == ({'value': 'x' * 70000} if mode == 'sequential' else None)
                assert collector.hits[1].request_sequence == 2
                assert requests[1] is responses[1].request
                assert requests[0] is not requests[1]
            assert collector.hits
            hit = collector.hits[0]
            assert len(collector.hits) == (2 if mode in {'budget', 'sequential', 'duplicate'} else 1)
            accepted = mode in {'ok', 'delayed', 'identity', 'identity_declared', 'declared', 'budget', 'sequential', 'iframe_hash', 'main_hash', 'duplicate_callback'}
            assert hit.resp_json == ({'value': 'x' * 70000} if accepted else None)
            assert commands.count('Network.getResponseBody') == (2 if mode == 'sequential' else int(accepted))
            if accepted:
                assert hit.body_size == len(server.payload)
            assert hit.raw_url == requests[0].url == native_url
            assert hit.request_frame is requests[0].frame is frame
            assert hit.request_frame_url == frame.url
            assert hit.request_sequence in ({1, 2} if mode == 'duplicate' else {1})
            assert server.posts == (2 if mode in {'budget', 'duplicate', 'redirect', 'sequential'} else 1)
            collector.detach(page)
            assert collector._history_observer is None and observer.records == {}
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('fault', ['post', 'method', 'url', 'frame', 'redirect', 'failed', 'missing', 'unfinished', 'duplicate', 'oversize', 'lying', 'records', 'reserve', 'root_url', 'frame_drift', 'wait_frame_drift', 'late_data', 'late_duplicate', 'late_reused_id', 'late_frame_drift', 'loader', 'missing_loader', 'response_loader', 'response_frame', 'document_origin', 'document_missing', 'native_frame', 'origin_port', 'origin_scheme', 'late_native_frame', 'late_foreign_origin', 'prebody_loader', 'prebody_duplicate', 'late_response_loader'])
def test_observer_rejects_unproved_body(fault):
    from types import SimpleNamespace
    observer = base._HistoryBodyObserver.__new__(base._HistoryBodyObserver)
    observer.url = 'https://www.ubot.com.tw/MyBank/IBKB010102'
    observer.records, observer.total, observer.bad = {}, 0, False
    observer.native_requests = []
    observer.WAIT_SECONDS = .01
    frame = SimpleNamespace(url='https://www.ubot.com.tw/frame')
    observer.page = SimpleNamespace(main_frame=frame, frames=[frame], wait_for_timeout=lambda ms: time.sleep(.001))
    loader = 'document'
    if fault == 'wait_frame_drift':
        def wait(ms):
            nonlocal loader
            loader = 'replacement'
            observer._event('loadingFinished', {'requestId': 'one'})
        observer.page.wait_for_timeout = wait
    commands = []
    def send(command, params=None):
        nonlocal loader
        commands.append(command)
        if command == 'Page.getFrameTree':
            if commands.count(command) == 2:
                if fault == 'prebody_loader':
                    loader = 'replacement'
                if fault == 'prebody_duplicate':
                    observer._request({**event, 'requestId': 'two'})
            url = frame.url
            if fault == 'frame_drift':
                frame.url += '#changed'
            return {'frameTree': {'frame': {'id': 'frame', 'loaderId': loader, 'url': url + ('#other' if fault == 'root_url' else '')}}}
        assert command == 'Network.getResponseBody'
        if fault == 'late_data':
            observer._event('dataReceived', {'requestId': 'one', 'dataLength': 0})
        if fault == 'late_response_loader':
            observer._event('responseReceived', {'requestId': 'one', 'frameId': 'frame', 'loaderId': 'replacement', 'response': {'status': 200, 'url': observer.url}})
        if fault in {'late_duplicate', 'late_reused_id'}:
            observer._request({**event, 'requestId': 'two' if fault == 'late_duplicate' else 'one'})
            # A reentrant second native callback must not consume the new ID.
            second_req = SimpleNamespace(**vars(req))
            second_resp = SimpleNamespace(request=second_req, url=observer.url, status=200)
            assert observer.read(second_resp, frame, frame.url, observer.LIMIT, 64) is None
            assert commands.count('Network.getResponseBody') == 1
        if fault == 'late_frame_drift':
            loader = 'replacement'
        if fault == 'late_native_frame':
            observer.page.frames = [SimpleNamespace(url=frame.url)]
        if fault == 'late_foreign_origin':
            frame.url = 'https://foreign.invalid/frame'
        return {'body': 'x' * (127 if fault == 'lying' else 128), 'base64Encoded': False}
    observer.session = SimpleNamespace(send=send)
    req = SimpleNamespace(url=observer.url, method='POST', post_data='a=1', frame=frame, redirected_from=None, redirected_to=None)
    resp = SimpleNamespace(request=req, url=observer.url, status=200)
    event = {'requestId': 'one', 'frameId': 'frame', 'loaderId': loader, 'documentURL': frame.url, 'request': {'url': observer.url, 'method': 'POST', 'postData': 'a=1'}}
    if fault == 'post':
        event['request']['postData'] = 'other'
    if fault == 'method':
        event['request']['method'] = 'GET'
    if fault == 'url':
        event['request']['url'] += '?other'
    if fault == 'frame':
        event['frameId'] = 'other'
    if fault == 'redirect':
        event['redirectResponse'] = {}
    if fault in {'loader', 'missing_loader'}:
        event['loaderId'] = 'replacement' if fault == 'loader' else ''
    if fault in {'document_origin', 'document_missing'}:
        event['documentURL'] = 'https://foreign.invalid/frame' if fault == 'document_origin' else ''
    if fault == 'native_frame':
        req.frame = SimpleNamespace(url=frame.url)
    if fault == 'origin_port':
        event['documentURL'] = 'https://www.ubot.com.tw:444/frame'
    if fault == 'origin_scheme':
        event['documentURL'] = 'http://www.ubot.com.tw/frame'
    if fault != 'missing':
        observer._request(event)
    observer._event('responseReceived', {'requestId': 'one', 'frameId': 'other' if fault == 'response_frame' else 'frame', 'loaderId': 'other' if fault == 'response_loader' else 'document', 'response': {'status': 200, 'url': observer.url}})
    observer._event('dataReceived', {'requestId': 'one', 'dataLength': observer.LIMIT + 1 if fault == 'oversize' else 128})
    if fault not in {'unfinished', 'wait_frame_drift'}:
        observer._event('loadingFinished', {'requestId': 'one'})
    if fault == 'failed':
        observer._event('loadingFailed', {'requestId': 'one'})
    if fault == 'duplicate':
        observer._request({**event, 'requestId': 'two'})
    if fault == 'records':
        for n in range(observer.MAX_RECORDS + 2):
            observer._request({**event, 'requestId': str(n)})
        assert len(observer.records) == observer.MAX_RECORDS
    result = observer.read(resp, frame, frame.url, observer.LIMIT, 64,
                           (lambda size: False) if fault == 'reserve' else None)
    assert result is None
    assert commands.count('Network.getResponseBody') == int(fault in {'lying', 'late_data', 'late_duplicate', 'late_reused_id', 'late_frame_drift', 'late_native_frame', 'late_foreign_origin', 'late_response_loader'})


@pytest.mark.parametrize('iframe', [False, True])
@pytest.mark.parametrize('change', ['hash', 'route', 'reload', 'foreign', 'replace'])
@pytest.mark.parametrize('phase', ['pending', 'body'])
def test_real_xhr_document_provenance(monkeypatch, iframe, change, phase):
    """Only same-document changes may retain the pending native XHR proof."""
    if change == 'replace' and not iframe:
        pytest.skip('replacement applies to child frames')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    server.payload = b'{"value":"synthetic document provenance"}'
    server.posts, server.mode = 0, 'document_pending'
    server.release = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(root)
            if iframe:
                page.evaluate("url => {let f=document.createElement('iframe'); f.src=url; document.body.append(f)}", root + '/child#original')
                page.wait_for_function("document.querySelector('iframe').contentDocument?.readyState === 'complete'")
            frame = page.frames[1] if iframe else page.main_frame
            original_url = frame.url
            observer = base._HistoryBodyObserver(page, root + '/history')
            observer.WAIT_SECONDS = 2
            original_send = observer.session.send
            commands, results, requests, provenance, errors = [], [], [], [], []
            changed = False

            def document():
                tree = original_send('Page.getFrameTree')['frameTree']
                return (tree['childFrames'][0] if iframe else tree)['frame']

            before = document()

            def send(command, params=None):
                nonlocal changed
                commands.append(command)
                target = 'Page.getFrameTree' if phase == 'pending' else 'Network.getResponseBody'
                result = original_send(command, params) if command == 'Network.getResponseBody' else None
                if command == target and not changed:
                    changed = True
                    if phase == 'pending':
                        assert not any(record['done'] for record in observer.records.values())
                    if change == 'hash':
                        frame.evaluate("location.hash = 'updated'")
                    elif change == 'route':
                        frame.evaluate("history.pushState({}, '', '/route?month=2#updated')")
                    elif change == 'reload':
                        with frame.expect_navigation():
                            frame.evaluate('location.reload()')
                    elif change == 'foreign':
                        frame.goto(original_url.replace('127.0.0.1', 'localhost'))
                    else:
                        page.evaluate("url => {document.querySelector('iframe').remove(); let f=document.createElement('iframe'); f.src=url; document.body.append(f)}", original_url)
                        page.wait_for_function("document.querySelector('iframe').contentDocument?.readyState === 'complete'")
                    after = document()
                    provenance.append((before['id'], before['loaderId'], after['id'], after['loaderId']))
                    server.release.set()
                # Retain real bytes so rejection cannot be a discarded-buffer error.
                return result if result is not None else original_send(command, params)

            monkeypatch.setattr(observer.session, 'send', send)
            page.on('request', lambda req: requests.append((req, req.frame.url)) if req.method == 'POST' else None)

            def response(resp):
                if resp.url != observer.url:
                    return
                if phase == 'body':
                    server.release.set()
                try:
                    results.append(observer.read(resp, frame, requests[0][1], observer.LIMIT, 0))
                except Exception as exc:
                    errors.append(exc)
                    results.append(None)

            page.on('response', response)
            frame.evaluate("url => {let xhr=new XMLHttpRequest(); xhr.open('POST',url); xhr.send('synthetic=1')}", observer.url)
            deadline = time.monotonic() + 5
            while not results and time.monotonic() < deadline:
                page.wait_for_timeout(20)
            assert len(results) == 1 and changed
            assert not errors
            assert requests[0][1] == original_url  # Never rewrite the bank validator's captured URL.
            assert requests[0][0].frame is frame
            old_id, old_loader, new_id, new_loader = provenance[0]
            if change in {'hash', 'route'}:
                assert (old_id, old_loader) == (new_id, new_loader)
                assert results == [server.payload]
                assert commands.count('Network.getResponseBody') == 1
            else:
                assert old_id != new_id or old_loader != new_loader
                assert results == [None]
            assert server.posts == 1
            observer.close()
            browser.close()
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('outcome', ['success', 'login_false', 'login_raise', 'collect_raise', 'logout_raise', 'dialog_raise'])
def test_run_detaches_observer_at_page_lifecycle_end(monkeypatch, tmp_path, outcome):
    from types import SimpleNamespace
    from unittest.mock import Mock

    monkeypatch.setattr(base, 'DATA_ROOT', tmp_path)
    actions, observers = [], []
    session = Mock()
    page = SimpleNamespace(on=Mock(), remove_listener=Mock(), context=SimpleNamespace(new_cdp_session=Mock(return_value=session)))

    class Crawler(base.BankCrawler):
        FETCH_INIT_SCRIPT = ''

        def _host_filter(self):
            return 'ubot.com.tw'

        def _credential_origin_allowed(self, page):
            return True

        def attach_shared_dialog_handler(self, page):
            observer = self.collector._history_observer
            observers.append(observer)
            observer._request({'requestId': 'private', 'frameId': 'frame', 'request': {
                'url': observer.url, 'method': 'POST', 'postData': 'synthetic=private',
            }})
            assert observer.records
            if outcome == 'dialog_raise':
                raise RuntimeError('synthetic dialog failure')

        def login(self, page):
            actions.append('login')
            if outcome == 'login_raise':
                raise RuntimeError('synthetic login failure')
            return outcome != 'login_false'

        _shared_login = login

        def collect(self, page, collector):
            actions.append('collect')
            if outcome == 'collect_raise':
                raise RuntimeError('synthetic collection failure')
            return base.BankCollectResult(card_bill_facts_ok=False)

        def logout(self, page):
            actions.append('logout')
            # Observation remains attached through the native logout action.
            assert self.collector._history_observer is observers[0]
            if outcome == 'logout_raise':
                raise RuntimeError('synthetic logout failure')

        def _execute_browser_flow(self, login_url, *, page_action, **kwargs):
            try:
                page_action(page)
            finally:
                # This is before the browser owner closes the page.
                assert self.collector._history_observer is None
                assert observers[0].records == {}
                assert observers[0].native_requests == []
                assert observers[0].bad
                session.detach.assert_called_once_with()
                assert session.remove_listener.call_count == 5
                assert page.remove_listener.call_count == 3

    crawler = Crawler(name='synthetic')
    if outcome == 'dialog_raise':
        with pytest.raises(RuntimeError, match='synthetic dialog failure'):
            crawler.run('https://synthetic.invalid')
    else:
        result = crawler.run('https://synthetic.invalid')
        assert ('error' in result) == (outcome in {'login_false', 'login_raise', 'collect_raise'})
    assert actions == ([] if outcome == 'dialog_raise' else ['login'] if outcome.startswith('login_') else ['login', 'collect', 'logout'])
