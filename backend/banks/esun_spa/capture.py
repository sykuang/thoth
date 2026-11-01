"""Product passive capture. Attach before login; never an authentication proof.
Callers must independently require full-valid DOM and recheck immediately before action.
Limits bound admitted decoded response bytes, NOT browser RSS. Bodies remain RAM-only.
"""

from backend.core.error_codes import exception_error_id, error_step, with_error_code
import re
from urllib.parse import urlsplit
from backend.core.base import (
    ApiHit,
    ResponseCollector,
    _HistoryBodyObserver,
    _strict_json_loads,
)

ORIGIN = "https://ebank.esunbank.com.tw"
PATHS = (
    "/esb/mib-auth-portal/cpo08/cpo08003/home/init",
    "/esb/mib-ctw-portal/ctw01/ctw01002/home/preQueryTWTransactionDetail",
    "/esb/mib-ctw-portal/ctw01/ctw01002/search/queryTWTransactionDetail",
    "/esb/mib-ccm-portal/ccm01/ccm01002/home/init",
    "/esb/mib-ccm-portal/ccm01/ccm01002/home/getOverviewData",
    "/esb/mib-ctw-portal/ctw01/ctw01002/home/continueQueryTWTransactionDetail",
)
QUERY_PATHS = (PATHS[2], PATHS[5])
LIMIT = 2 * 1024 * 1024
TOTAL = 64 * 1024 * 1024


def _path(url):
    return next((p for p in PATHS if url == ORIGIN + p), None)


def _origin(url):
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc == "ebank.esunbank.com.tw"


def _json_type(value):
    return (
        isinstance(value, str)
        and re.fullmatch('application/json(?:\\s*;\\s*charset\\s*=\\s*(?:utf-8|"utf-8"))?', value, re.I) is not None
    )


def _json(raw):
    return _strict_json_loads(raw)


class SpaCollector(ResponseCollector):
    MAX_QUERY_REQUESTS = 512  # Shared bounded budget across all accounts and windows.

    def __init__(self):
        super().__init__("esunbank.com.tw")
        self.observers = {}
        self.page = None
        self.owned = {}
        self._latest_spa = {}
        self.admitted_bytes = 0
        self.rejected = 0
        self._attachment = None
        self._continuation_observation = None
        self.publication_checks = []
        self._attached_handlers = {}

    def attach(self, page):
        if self.page is page:
            return
        if self.page is not None:
            raise with_error_code(ValueError('already attached'), 'ESUN-07-401-001')
        self.page = page
        self._attachment = object()
        try:
            for path in PATHS:
                observer = _HistoryBodyObserver(page, ORIGIN + path)
                observer.LIMIT, observer.MAX_RECORDS = (LIMIT, self.MAX_QUERY_REQUESTS)
                observer.TOTAL_LIMIT = TOTAL
                self.observers[path] = observer
                with error_step('ESUN-07-422-001'):
                    observer.start()
            for name, handler in self._handlers():
                with error_step('ESUN-07-423-001'):
                    page.on(name, handler)
                self._attached_handlers[name] = handler
        except BaseException:
            try:
                with error_step('ESUN-07-428-001'):
                    self.detach(page)
            except Exception:
                pass
            raise

    def _handlers(self):
        return (
            ("request", self._request_handler),
            ("requestfailed", self._request_failed_handler),
            ("response", self._response_handler),
        )

    def detach(self, page=None):
        if self.page is None and not self.observers and not self._attached_handlers:
            return
        if page is not None and page is not self.page:
            raise with_error_code(ValueError('foreign page'), 'ESUN-07-402-001')
        error = None
        cleanup_page = self.page
        try:
            # Keep all listeners and receipts live until the last CDP detach returns.
            for observer in self.observers.values():
                try:
                    with error_step('ESUN-07-424-001'):
                        observer.disconnect()
                except Exception as exc:
                    error = error or exc
            for name, handler in tuple(self._attached_handlers.items()):
                try:
                    if cleanup_page is None:
                        raise with_error_code(RuntimeError('missing cleanup page'), 'ESUN-07-414-001')
                    with error_step('ESUN-07-425-001'):
                        cleanup_page.remove_listener(name, handler)
                except Exception as exc:
                    error = error or exc
                else:
                    self._attached_handlers.pop(name, None)
            try:
                for prove in self.publication_checks:
                    prove()
            except Exception as exc:
                error = error or exc
            # close() is now local-only: no dispatch after this final proof.
            for path, observer in tuple(self.observers.items()):
                try:
                    with error_step('ESUN-07-426-001'):
                        observer.close()
                except Exception as exc:
                    error = error or exc
                else:
                    self.observers.pop(path, None)
        finally:
            self._continuation_observation = None
            self.owned.clear()
            self.hits.clear()
            self._latest_spa.clear()
            self._attachment = None
            for data in (
                self._requests,
                self._request_main_frame,
                self._request_frame_urls,
                self._request_frames,
            ):
                data.clear()
            # A failed listener removal alone still needs its page for retry. CDP-only
            # cleanup failures retain observers but not the page or financial payloads.
            if not self._attached_handlers:
                self.page = None
        if error is not None:
            raise with_error_code(error, 'ESUN-07-403-001')

    def snapshot(self):
        if self.page is None:
            raise with_error_code(ValueError('not attached'), 'ESUN-07-404-001')
        return {
            "collector": self,
            "attachment": self._attachment,
            "sequence": self.request_sequence,
            "counts": {p: self.issued_count(p) for p in PATHS},
            "main_frame": self.page.main_frame,
        }

    def query_issued_count(self):
        return sum((self.issued_count(path) for path in QUERY_PATHS))

    def _on_request(self, req):
        parsed = urlsplit(req.url)
        path = parsed.path if _origin(req.url) and parsed.path in PATHS else None
        if path is None or self.page is None:
            return
        self._request_sequence += 1
        sequence = self._request_sequence
        for endpoint in {path, path.rsplit("/", 1)[-1]}:
            self._issued_endpoint_counts[endpoint] = self.issued_count(endpoint) + 1
        self._latest_spa[path] = (sequence, None)
        self.hits[:] = [h for h in self.hits if h.url != ORIGIN + path]
        if path in QUERY_PATHS:
            self._continuation_observation = None
        if (
            _path(req.url) is None
            or self.issued_count(path) > self.MAX_QUERY_REQUESTS
            or (path in QUERY_PATHS and self.query_issued_count() > self.MAX_QUERY_REQUESTS)
        ):
            return
        try:
            frame = req.frame
            frame_url = frame.url
            main = frame is self.page.main_frame and frame.page is self.page
        except Exception:
            return
        self._requests[id(req)] = sequence
        self._request_main_frame[id(req)] = main
        self._request_frame_urls[id(req)] = frame_url if type(frame_url) is str else ""
        self._request_frames[id(req)] = frame
        self.owned[id(req)] = req

    def _on_request_failed(self, req):
        observation = self._continuation_observation
        if observation is not None and observation[2]._native_request is req:
            self._continuation_observation = None
        path = _path(req.url)
        observer = self.observers.get(path)
        if observer is not None:
            for record in observer.records.values():
                if record.get("native_request") is req:
                    record["bad"] = True  # Retain historical proof, but revoke this request.
        sequence = self._requests.get(id(req), 0)
        latest_sequence, latest_hit = self._latest_spa.get(path, (0, None))
        if latest_hit is not None and getattr(latest_hit, "_native_request", None) is req:
            sequence = latest_sequence
        if path is not None and self._latest_spa.get(path, (0,))[0] == sequence:
            self._latest_spa[path] = (sequence, None)
            self.hits[:] = [h for h in self.hits if h.url != req.url]
        self.owned.pop(id(req), None)
        super()._on_request_failed(req)

    def _reserve(self, size):
        if type(size) is not int or not 0 <= size <= LIMIT or self.admitted_bytes + size > TOTAL:
            return False
        self.admitted_bytes += size
        return True

    def _on_response(self, resp):
        req = resp.request
        path = _path(req.url)
        if path is None or self.page is None:
            return
        sequence = self._requests.get(id(req), 0)
        frame = self._request_frames.get(id(req))
        frame_url = self._request_frame_urls.get(id(req), "")
        count = self.issued_count(path)
        attachment, query_count = (self._attachment, self.query_issued_count())
        try:
            if self.owned.get(id(req)) is not req:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-001', PATHS[1]: 'ESUN-07-405-101', PATHS[2]: 'ESUN-07-405-201', PATHS[3]: 'ESUN-07-405-301', PATHS[4]: 'ESUN-07-405-401', PATHS[5]: 'ESUN-07-405-501'}.get(path, 'ESUN-07-405-001'))
            if sequence <= 0:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-002', PATHS[1]: 'ESUN-07-405-102', PATHS[2]: 'ESUN-07-405-202', PATHS[3]: 'ESUN-07-405-302', PATHS[4]: 'ESUN-07-405-402', PATHS[5]: 'ESUN-07-405-502'}.get(path, 'ESUN-07-405-002'))
            if self._latest_spa.get(path) != (sequence, None):
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-003', PATHS[1]: 'ESUN-07-405-103', PATHS[2]: 'ESUN-07-405-203', PATHS[3]: 'ESUN-07-405-303', PATHS[4]: 'ESUN-07-405-403', PATHS[5]: 'ESUN-07-405-503'}.get(path, 'ESUN-07-405-003'))
            if count > self.MAX_QUERY_REQUESTS:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-004', PATHS[1]: 'ESUN-07-405-104', PATHS[2]: 'ESUN-07-405-204', PATHS[3]: 'ESUN-07-405-304', PATHS[4]: 'ESUN-07-405-404', PATHS[5]: 'ESUN-07-405-504'}.get(path, 'ESUN-07-405-004'))
            if path in QUERY_PATHS and self.query_issued_count() > self.MAX_QUERY_REQUESTS:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-005', PATHS[1]: 'ESUN-07-405-105', PATHS[2]: 'ESUN-07-405-205', PATHS[3]: 'ESUN-07-405-305', PATHS[4]: 'ESUN-07-405-405', PATHS[5]: 'ESUN-07-405-505'}.get(path, 'ESUN-07-405-005'))
            if req.frame is not frame:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-006', PATHS[1]: 'ESUN-07-405-106', PATHS[2]: 'ESUN-07-405-206', PATHS[3]: 'ESUN-07-405-306', PATHS[4]: 'ESUN-07-405-406', PATHS[5]: 'ESUN-07-405-506'}.get(path, 'ESUN-07-405-006'))
            if frame is not self.page.main_frame:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-007', PATHS[1]: 'ESUN-07-405-107', PATHS[2]: 'ESUN-07-405-207', PATHS[3]: 'ESUN-07-405-307', PATHS[4]: 'ESUN-07-405-407', PATHS[5]: 'ESUN-07-405-507'}.get(path, 'ESUN-07-405-007'))
            if not self._request_main_frame.get(id(req)):
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-008', PATHS[1]: 'ESUN-07-405-108', PATHS[2]: 'ESUN-07-405-208', PATHS[3]: 'ESUN-07-405-308', PATHS[4]: 'ESUN-07-405-408', PATHS[5]: 'ESUN-07-405-508'}.get(path, 'ESUN-07-405-008'))
            if frame.page is not self.page:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-009', PATHS[1]: 'ESUN-07-405-109', PATHS[2]: 'ESUN-07-405-209', PATHS[3]: 'ESUN-07-405-309', PATHS[4]: 'ESUN-07-405-409', PATHS[5]: 'ESUN-07-405-509'}.get(path, 'ESUN-07-405-009'))
            if not _origin(frame_url):
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-010', PATHS[1]: 'ESUN-07-405-110', PATHS[2]: 'ESUN-07-405-210', PATHS[3]: 'ESUN-07-405-310', PATHS[4]: 'ESUN-07-405-410', PATHS[5]: 'ESUN-07-405-510'}.get(path, 'ESUN-07-405-010'))
            if not _origin(frame.url):
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-011', PATHS[1]: 'ESUN-07-405-111', PATHS[2]: 'ESUN-07-405-211', PATHS[3]: 'ESUN-07-405-311', PATHS[4]: 'ESUN-07-405-411', PATHS[5]: 'ESUN-07-405-511'}.get(path, 'ESUN-07-405-011'))
            if resp.url != ORIGIN + path:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-012', PATHS[1]: 'ESUN-07-405-112', PATHS[2]: 'ESUN-07-405-212', PATHS[3]: 'ESUN-07-405-312', PATHS[4]: 'ESUN-07-405-412', PATHS[5]: 'ESUN-07-405-512'}.get(path, 'ESUN-07-405-012'))
            if req.method != "POST":
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-013', PATHS[1]: 'ESUN-07-405-113', PATHS[2]: 'ESUN-07-405-213', PATHS[3]: 'ESUN-07-405-313', PATHS[4]: 'ESUN-07-405-413', PATHS[5]: 'ESUN-07-405-513'}.get(path, 'ESUN-07-405-013'))
            if resp.status != 200:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-014', PATHS[1]: 'ESUN-07-405-114', PATHS[2]: 'ESUN-07-405-214', PATHS[3]: 'ESUN-07-405-314', PATHS[4]: 'ESUN-07-405-414', PATHS[5]: 'ESUN-07-405-514'}.get(path, 'ESUN-07-405-014'))
            if req.redirected_from is not None:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-015', PATHS[1]: 'ESUN-07-405-115', PATHS[2]: 'ESUN-07-405-215', PATHS[3]: 'ESUN-07-405-315', PATHS[4]: 'ESUN-07-405-415', PATHS[5]: 'ESUN-07-405-515'}.get(path, 'ESUN-07-405-015'))
            if req.redirected_to is not None:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-016', PATHS[1]: 'ESUN-07-405-116', PATHS[2]: 'ESUN-07-405-216', PATHS[3]: 'ESUN-07-405-316', PATHS[4]: 'ESUN-07-405-416', PATHS[5]: 'ESUN-07-405-516'}.get(path, 'ESUN-07-405-016'))
            if not _json_type(req.headers.get("content-type", "")):
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-017', PATHS[1]: 'ESUN-07-405-117', PATHS[2]: 'ESUN-07-405-217', PATHS[3]: 'ESUN-07-405-317', PATHS[4]: 'ESUN-07-405-417', PATHS[5]: 'ESUN-07-405-517'}.get(path, 'ESUN-07-405-017'))
            if not _json_type(resp.headers.get("content-type", "")):
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-405-018', PATHS[1]: 'ESUN-07-405-118', PATHS[2]: 'ESUN-07-405-218', PATHS[3]: 'ESUN-07-405-318', PATHS[4]: 'ESUN-07-405-418', PATHS[5]: 'ESUN-07-405-518'}.get(path, 'ESUN-07-405-018'))
            post = req.post_data
            if not isinstance(post, str):
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-406-001', PATHS[1]: 'ESUN-07-406-101', PATHS[2]: 'ESUN-07-406-201', PATHS[3]: 'ESUN-07-406-301', PATHS[4]: 'ESUN-07-406-401', PATHS[5]: 'ESUN-07-406-501'}.get(path, 'ESUN-07-406-001'))
            if len(post.encode("utf-8")) > 16384:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-406-002', PATHS[1]: 'ESUN-07-406-102', PATHS[2]: 'ESUN-07-406-202', PATHS[3]: 'ESUN-07-406-302', PATHS[4]: 'ESUN-07-406-402', PATHS[5]: 'ESUN-07-406-502'}.get(path, 'ESUN-07-406-002'))
            request_body = _json(post)
            if type(request_body) is not dict:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-407-001', PATHS[1]: 'ESUN-07-407-101', PATHS[2]: 'ESUN-07-407-201', PATHS[3]: 'ESUN-07-407-301', PATHS[4]: 'ESUN-07-407-401', PATHS[5]: 'ESUN-07-407-501'}.get(path, 'ESUN-07-407-001'))
            with error_step({PATHS[0]: 'ESUN-07-427-001', PATHS[1]: 'ESUN-07-427-101', PATHS[2]: 'ESUN-07-427-201', PATHS[3]: 'ESUN-07-427-301', PATHS[4]: 'ESUN-07-427-401', PATHS[5]: 'ESUN-07-427-501'}.get(path, 'ESUN-07-427-001')):
                raw = self.observers[path].read(
                    resp, frame, frame_url, lambda: TOTAL - self.admitted_bytes, 1, self._reserve
                )
            if raw is None:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-408-001', PATHS[1]: 'ESUN-07-408-101', PATHS[2]: 'ESUN-07-408-201', PATHS[3]: 'ESUN-07-408-301', PATHS[4]: 'ESUN-07-408-401', PATHS[5]: 'ESUN-07-408-501'}.get(path, 'ESUN-07-408-001'))
            if len(raw) > LIMIT:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-408-002', PATHS[1]: 'ESUN-07-408-102', PATHS[2]: 'ESUN-07-408-202', PATHS[3]: 'ESUN-07-408-302', PATHS[4]: 'ESUN-07-408-402', PATHS[5]: 'ESUN-07-408-502'}.get(path, 'ESUN-07-408-002'))
            body = _json(raw)
            observation_only = (
                path == PATHS[5]
                and type(body) is dict
                and (type(body.get("resultCode")) is str)
                and (body["resultCode"] != "0000")
            )
            if type(body) is not dict:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-409-001', PATHS[1]: 'ESUN-07-409-101', PATHS[2]: 'ESUN-07-409-201', PATHS[3]: 'ESUN-07-409-301', PATHS[4]: 'ESUN-07-409-401', PATHS[5]: 'ESUN-07-409-501'}.get(path, 'ESUN-07-409-001'))
            if type(body.get("resultCode")) is not str:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-409-002', PATHS[1]: 'ESUN-07-409-102', PATHS[2]: 'ESUN-07-409-202', PATHS[3]: 'ESUN-07-409-302', PATHS[4]: 'ESUN-07-409-402', PATHS[5]: 'ESUN-07-409-502'}.get(path, 'ESUN-07-409-002'))
            if not observation_only and (body["resultCode"] != "0000" or type(body.get("resultBody")) is not dict):
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-409-003', PATHS[1]: 'ESUN-07-409-103', PATHS[2]: 'ESUN-07-409-203', PATHS[3]: 'ESUN-07-409-303', PATHS[4]: 'ESUN-07-409-403', PATHS[5]: 'ESUN-07-409-503'}.get(path, 'ESUN-07-409-003'))
            if self.issued_count(path) != count:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-410-001', PATHS[1]: 'ESUN-07-410-101', PATHS[2]: 'ESUN-07-410-201', PATHS[3]: 'ESUN-07-410-301', PATHS[4]: 'ESUN-07-410-401', PATHS[5]: 'ESUN-07-410-501'}.get(path, 'ESUN-07-410-001'))
            if path in QUERY_PATHS and self.query_issued_count() > self.MAX_QUERY_REQUESTS:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-410-002', PATHS[1]: 'ESUN-07-410-102', PATHS[2]: 'ESUN-07-410-202', PATHS[3]: 'ESUN-07-410-302', PATHS[4]: 'ESUN-07-410-402', PATHS[5]: 'ESUN-07-410-502'}.get(path, 'ESUN-07-410-002'))
            if self._latest_spa.get(path) != (sequence, None):
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-410-003', PATHS[1]: 'ESUN-07-410-103', PATHS[2]: 'ESUN-07-410-203', PATHS[3]: 'ESUN-07-410-303', PATHS[4]: 'ESUN-07-410-403', PATHS[5]: 'ESUN-07-410-503'}.get(path, 'ESUN-07-410-003'))
            if self.page.main_frame is not frame:
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-410-004', PATHS[1]: 'ESUN-07-410-104', PATHS[2]: 'ESUN-07-410-204', PATHS[3]: 'ESUN-07-410-304', PATHS[4]: 'ESUN-07-410-404', PATHS[5]: 'ESUN-07-410-504'}.get(path, 'ESUN-07-410-004'))
            if not _origin(frame.url):
                raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-410-005', PATHS[1]: 'ESUN-07-410-105', PATHS[2]: 'ESUN-07-410-205', PATHS[3]: 'ESUN-07-410-305', PATHS[4]: 'ESUN-07-410-405', PATHS[5]: 'ESUN-07-410-505'}.get(path, 'ESUN-07-410-005'))
            hit = ApiHit(
                url=req.url,
                raw_url=req.url,
                method="POST",
                status=200,
                req_body=request_body,
                resp_json=body if observation_only else {"resultCode": "0000", "resultBody": body["resultBody"]},
                content_type=resp.headers["content-type"],
                body_size=len(raw),
                request_sequence=sequence,
                main_frame_request=True,
                request_frame_url=frame_url,
                request_frame=frame,
            )
            hit._native_request = req
            if observation_only:
                if self._attachment is not attachment:
                    raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-411-001', PATHS[1]: 'ESUN-07-411-101', PATHS[2]: 'ESUN-07-411-201', PATHS[3]: 'ESUN-07-411-301', PATHS[4]: 'ESUN-07-411-401', PATHS[5]: 'ESUN-07-411-501'}.get(path, 'ESUN-07-411-001'))
                if self.owned.get(id(req)) is not req:
                    raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-411-002', PATHS[1]: 'ESUN-07-411-102', PATHS[2]: 'ESUN-07-411-202', PATHS[3]: 'ESUN-07-411-302', PATHS[4]: 'ESUN-07-411-402', PATHS[5]: 'ESUN-07-411-502'}.get(path, 'ESUN-07-411-002'))
                if self._requests.get(id(req)) != sequence:
                    raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-411-003', PATHS[1]: 'ESUN-07-411-103', PATHS[2]: 'ESUN-07-411-203', PATHS[3]: 'ESUN-07-411-303', PATHS[4]: 'ESUN-07-411-403', PATHS[5]: 'ESUN-07-411-503'}.get(path, 'ESUN-07-411-003'))
                if frame.page is not self.page:
                    raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-411-004', PATHS[1]: 'ESUN-07-411-104', PATHS[2]: 'ESUN-07-411-204', PATHS[3]: 'ESUN-07-411-304', PATHS[4]: 'ESUN-07-411-404', PATHS[5]: 'ESUN-07-411-504'}.get(path, 'ESUN-07-411-004'))
                if self._latest_spa.get(PATHS[2], (0,))[0] > sequence:
                    raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-411-005', PATHS[1]: 'ESUN-07-411-105', PATHS[2]: 'ESUN-07-411-205', PATHS[3]: 'ESUN-07-411-305', PATHS[4]: 'ESUN-07-411-405', PATHS[5]: 'ESUN-07-411-505'}.get(path, 'ESUN-07-411-005'))
                if self.query_issued_count() != query_count:
                    raise with_error_code(ValueError('rejected'), {PATHS[0]: 'ESUN-07-411-006', PATHS[1]: 'ESUN-07-411-106', PATHS[2]: 'ESUN-07-411-206', PATHS[3]: 'ESUN-07-411-306', PATHS[4]: 'ESUN-07-411-406', PATHS[5]: 'ESUN-07-411-506'}.get(path, 'ESUN-07-411-006'))
                self._continuation_observation = (attachment, query_count, hit)
                return
            self.hits.append(hit)
            self._latest_spa[path] = (sequence, hit)
        except Exception:
            observation = self._continuation_observation
            if observation is not None and observation[2]._native_request is req:
                self._continuation_observation = None
            self.rejected += 1
            if sequence == 0 or self._latest_spa.get(path, (0,))[0] == sequence:
                self._latest_spa[path] = (self._latest_spa.get(path, (sequence,))[0], None)
                self.hits[:] = [h for h in self.hits if h.url != req.url]
        finally:
            self.owned.pop(id(req), None)
            super()._on_request_failed(req)

    def _require_current_continuation_observation(self, baseline):
        """Private RAM envelope/request only. No terminal or completion inference."""
        path = PATHS[5]
        observation = self._continuation_observation

        def valid():
            if observation is None:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-505-001')
            if self._continuation_observation is not observation:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-505-002')
            attachment, query_count, hit = observation
            request = hit._native_request
            observer = self.observers.get(path)
            if self.page is None:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-001')
            if self._attachment is not attachment:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-002')
            if baseline.get("collector") is not self:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-003')
            if baseline.get("attachment") is not attachment:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-004')
            if baseline.get("main_frame") is not self.page.main_frame:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-005')
            if hit.request_frame is not self.page.main_frame:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-006')
            if hit.request_frame.page is not self.page:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-007')
            if not _origin(hit.request_frame_url):
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-008')
            if not _origin(hit.request_frame.url):
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-009')
            if not hit.main_frame_request:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-010')
            if self.issued_count(path) != baseline["counts"][path] + 1:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-011')
            if self.query_issued_count() != query_count:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-012')
            if query_count > self.MAX_QUERY_REQUESTS:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-013')
            if self._latest_spa.get(path) != (hit.request_sequence, None):
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-014')
            if hit.request_sequence <= baseline["sequence"]:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-015')
            if request.frame is not hit.request_frame:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-016')
            if request.url != ORIGIN + path:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-017')
            if request.method != "POST":
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-018')
            if request.redirected_from is not None:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-019')
            if request.redirected_to is not None:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-020')
            if hit.url != ORIGIN + path:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-021')
            if hit.raw_url != hit.url:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-022')
            if hit.status != 200:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-023')
            if hit.method != "POST":
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-024')
            if hit.redirected:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-025')
            if not _json_type(hit.content_type):
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-026')
            if type(hit.req_body) is not dict:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-027')
            if type(hit.resp_json) is not dict:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-028')
            if type(hit.resp_json.get("resultCode")) is not str:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-029')
            if hit.resp_json["resultCode"] == "0000":
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-030')
            if observer is None:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-031')
            if observer.bad:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-506-032')
            records = [r for r in observer.records.values() if r.get("native_request") is request]
            if len(records) != 1:
                raise with_error_code(ValueError('no current owned continuation observation'), 'ESUN-07-507-001')
            record = records[0]
            return not record["bad"] and record["used"] and record["done"] and record["response"]

        try:
            for _ in range(2):
                if not valid():
                    raise with_error_code(ValueError(), 'ESUN-07-412-001')
                hit = observation[2]
                observer = self.observers[path]
                record = next((r for r in observer.records.values() if r.get("native_request") is hit._native_request))
                document = observer._document(hit.request_frame, hit.request_frame_url)
                if document != (record["key"][3], record["loader"]):
                    raise with_error_code(ValueError(), 'ESUN-07-413-001')
                if not valid():
                    raise with_error_code(ValueError(), 'ESUN-07-413-002')
            return (hit.resp_json, hit.req_body)
        except Exception as source_error:
            if self._continuation_observation is observation:
                self._continuation_observation = None
            raise with_error_code(ValueError("no current owned continuation observation"), exception_error_id(source_error, bank="esun") or 'ESUN-07-415-001') from None


def require_current_success(collector, baseline, path):
    """RAM-only (resultBody, requestBody); not a login/authentication decision."""

    def valid():
        if path not in PATHS:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-501-001')
        if baseline.get("collector") is not collector:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-501-002')
        if collector.page is None:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-501-003')
        if baseline.get("attachment") is not collector._attachment:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-501-004')
        if baseline.get("main_frame") is not collector.page.main_frame:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-501-005')
        if collector.issued_count(path) != baseline["counts"][path] + 1:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-501-006')
        if path in QUERY_PATHS and collector.query_issued_count() > collector.MAX_QUERY_REQUESTS:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-501-007')
        sequence, hit = collector._latest_spa.get(path, (0, None))
        if hit is None:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-001')
        if sequence <= baseline["sequence"]:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-002')
        if hit.request_sequence != sequence:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-003')
        if not any((h is hit for h in collector.hits)):
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-004')
        if hit.request_frame is not collector.page.main_frame:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-005')
        if not hit.main_frame_request:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-006')
        if not _origin(hit.request_frame_url):
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-007')
        if not _origin(hit.request_frame.url):
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-008')
        if hit.raw_url != ORIGIN + path:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-009')
        if hit.url != ORIGIN + path:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-010')
        if hit.method != "POST":
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-011')
        if hit.status != 200:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-012')
        if hit.redirected:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-013')
        if not _json_type(hit.content_type):
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-014')
        if type(hit.req_body) is not dict:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-015')
        if type(hit.resp_json) is not dict:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-016')
        if type(hit.resp_json.get("resultCode")) is not str:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-017')
        if hit.resp_json["resultCode"] != "0000":
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-018')
        if type(hit.resp_json.get("resultBody")) is not dict:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-502-019')
        observer = collector.observers.get(path)
        request = getattr(hit, "_native_request", None)
        records = (
            [] if observer is None else [r for r in observer.records.values() if r.get("native_request") is request]
        )
        if observer is None:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-503-001')
        if observer.bad:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-503-002')
        if request is None:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-503-003')
        if len(records) != 1:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-503-004')
        if records[0]["bad"]:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-503-005')
        if not records[0]["used"]:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-503-006')
        if not records[0]["done"]:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-503-007')
        if not records[0]["response"]:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-503-008')
        if request.redirected_from is not None:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-503-009')
        if request.redirected_to is not None:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-503-010')
        document = observer._document(hit.request_frame, hit.request_frame_url)
        if document != (records[0]["key"][3], records[0]["loader"]):
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-504-001')
        if observer.bad:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-504-002')
        if records[0]["bad"]:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-504-003')
        if collector.issued_count(path) != baseline["counts"][path] + 1:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-504-004')
        if path in QUERY_PATHS and collector.query_issued_count() > collector.MAX_QUERY_REQUESTS:
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-504-005')
        if collector._latest_spa.get(path) != (sequence, hit):
            raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-504-006')
        return hit

    hit = valid()
    if hit is None:
        raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-400-001')
    if valid() is not hit:
        raise with_error_code(ValueError('no current owned SPA success'), 'ESUN-07-400-002')
    return (hit.resp_json["resultBody"], hit.req_body)
