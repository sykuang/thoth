"""Product passive capture. Attach before login; never an authentication proof.
Callers must independently require full-valid DOM and recheck immediately before action.
Limits bound admitted decoded response bytes, NOT browser RSS. Bodies remain RAM-only.
"""

import json
import re
from urllib.parse import urlsplit
from backend.core.base import ApiHit, ResponseCollector, _HistoryBodyObserver

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

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("invalid JSON")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("invalid JSON")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


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

    def attach(self, page):
        if self.page is page:
            return
        if self.page is not None:
            raise ValueError("already attached")
        self.page = page
        self._attachment = object()
        try:
            for path in PATHS:
                observer = _HistoryBodyObserver(page, ORIGIN + path)
                observer.LIMIT, observer.MAX_RECORDS = (LIMIT, self.MAX_QUERY_REQUESTS)
                observer.TOTAL_LIMIT = TOTAL
                self.observers[path] = observer
            for name, handler in self._handlers():
                page.on(name, handler)
        except BaseException:
            try:
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
        if self.page is None:
            return
        if page is not None and page is not self.page:
            raise ValueError("foreign page")
        error = None
        # Keep all listeners and receipts live until the last CDP detach returns.
        for observer in self.observers.values():
            try:
                observer.disconnect()
            except Exception as exc:
                error = error or exc
        for name, handler in self._handlers():
            try:
                self.page.remove_listener(name, handler)
            except Exception as exc:
                error = error or exc
        try:
            for prove in self.publication_checks:
                prove()
        except Exception as exc:
            error = error or exc
        # close() is now local-only: no dispatch after this final proof.
        for observer in self.observers.values():
            try:
                observer.close()
            except Exception as exc:
                error = error or exc
        self.observers.clear()
        # Keep receipts through publication: detach invalidates, never erases proof.
        self._continuation_observation = None
        self.owned.clear()
        self.hits.clear()
        self._latest_spa.clear()
        self._attachment = None
        for data in (self._requests, self._request_main_frame, self._request_frame_urls, self._request_frames):
            data.clear()
        self.page = None
        if error is not None:
            raise error

    def snapshot(self):
        if self.page is None:
            raise ValueError("not attached")
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
            if (
                self.owned.get(id(req)) is not req
                or sequence <= 0
                or self._latest_spa.get(path) != (sequence, None)
                or (count > self.MAX_QUERY_REQUESTS)
                or (path in QUERY_PATHS and self.query_issued_count() > self.MAX_QUERY_REQUESTS)
                or (req.frame is not frame)
                or (frame is not self.page.main_frame)
                or (not self._request_main_frame.get(id(req)))
                or (frame.page is not self.page)
                or (not _origin(frame_url))
                or (not _origin(frame.url))
                or (resp.url != ORIGIN + path)
                or (req.method != "POST")
                or (resp.status != 200)
                or (req.redirected_from is not None)
                or (req.redirected_to is not None)
                or (not _json_type(req.headers.get("content-type", "")))
                or (not _json_type(resp.headers.get("content-type", "")))
            ):
                raise ValueError("rejected")
            post = req.post_data
            if not isinstance(post, str) or len(post.encode("utf-8")) > 16384:
                raise ValueError("rejected")
            request_body = _json(post)
            if type(request_body) is not dict:
                raise ValueError("rejected")
            raw = self.observers[path].read(
                resp, frame, frame_url, lambda: TOTAL - self.admitted_bytes, 1, self._reserve
            )
            if raw is None or len(raw) > LIMIT:
                raise ValueError("rejected")
            body = _json(raw)
            observation_only = (
                path == PATHS[5]
                and type(body) is dict
                and (type(body.get("resultCode")) is str)
                and (body["resultCode"] != "0000")
            )
            if (
                type(body) is not dict
                or type(body.get("resultCode")) is not str
                or (not observation_only and (body["resultCode"] != "0000" or type(body.get("resultBody")) is not dict))
            ):
                raise ValueError("rejected")
            if (
                self.issued_count(path) != count
                or (path in QUERY_PATHS and self.query_issued_count() > self.MAX_QUERY_REQUESTS)
                or self._latest_spa.get(path) != (sequence, None)
                or (self.page.main_frame is not frame)
                or (not _origin(frame.url))
            ):
                raise ValueError("rejected")
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
                if (
                    self._attachment is not attachment
                    or self.owned.get(id(req)) is not req
                    or self._requests.get(id(req)) != sequence
                    or (frame.page is not self.page)
                    or (self._latest_spa.get(PATHS[2], (0,))[0] > sequence)
                    or (self.query_issued_count() != query_count)
                ):
                    raise ValueError("rejected")
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
            if observation is None or self._continuation_observation is not observation:
                return False
            attachment, query_count, hit = observation
            request = hit._native_request
            observer = self.observers.get(path)
            if (
                self.page is None
                or self._attachment is not attachment
                or baseline.get("collector") is not self
                or (baseline.get("attachment") is not attachment)
                or (baseline.get("main_frame") is not self.page.main_frame)
                or (hit.request_frame is not self.page.main_frame)
                or (hit.request_frame.page is not self.page)
                or (not _origin(hit.request_frame_url))
                or (not _origin(hit.request_frame.url))
                or (not hit.main_frame_request)
                or (self.issued_count(path) != baseline["counts"][path] + 1)
                or (self.query_issued_count() != query_count)
                or (query_count > self.MAX_QUERY_REQUESTS)
                or (self._latest_spa.get(path) != (hit.request_sequence, None))
                or (hit.request_sequence <= baseline["sequence"])
                or (request.frame is not hit.request_frame)
                or (request.url != ORIGIN + path)
                or (request.method != "POST")
                or (request.redirected_from is not None)
                or (request.redirected_to is not None)
                or (hit.url != ORIGIN + path)
                or (hit.raw_url != hit.url)
                or (hit.status != 200)
                or (hit.method != "POST")
                or hit.redirected
                or (not _json_type(hit.content_type))
                or (type(hit.req_body) is not dict)
                or (type(hit.resp_json) is not dict)
                or (type(hit.resp_json.get("resultCode")) is not str)
                or (hit.resp_json["resultCode"] == "0000")
                or (observer is None)
                or observer.bad
            ):
                return False
            records = [r for r in observer.records.values() if r.get("native_request") is request]
            if len(records) != 1:
                return False
            record = records[0]
            return not record["bad"] and record["used"] and record["done"] and record["response"]

        try:
            for _ in range(2):
                if not valid():
                    raise ValueError
                hit = observation[2]
                observer = self.observers[path]
                record = next((r for r in observer.records.values() if r.get("native_request") is hit._native_request))
                document = observer._document(hit.request_frame, hit.request_frame_url)
                if document != (record["key"][3], record["loader"]) or not valid():
                    raise ValueError
            return (hit.resp_json, hit.req_body)
        except Exception:
            if self._continuation_observation is observation:
                self._continuation_observation = None
            raise ValueError("no current owned continuation observation") from None


def require_current_success(collector, baseline, path):
    """RAM-only (resultBody, requestBody); not a login/authentication decision."""

    def valid():
        if (
            path not in PATHS
            or baseline.get("collector") is not collector
            or collector.page is None
            or (baseline.get("attachment") is not collector._attachment)
            or (baseline.get("main_frame") is not collector.page.main_frame)
            or (collector.issued_count(path) != baseline["counts"][path] + 1)
            or (path in QUERY_PATHS and collector.query_issued_count() > collector.MAX_QUERY_REQUESTS)
        ):
            return None
        sequence, hit = collector._latest_spa.get(path, (0, None))
        if (
            hit is None
            or sequence <= baseline["sequence"]
            or hit.request_sequence != sequence
            or (not any((h is hit for h in collector.hits)))
            or (hit.request_frame is not collector.page.main_frame)
            or (not hit.main_frame_request)
            or (not _origin(hit.request_frame_url))
            or (not _origin(hit.request_frame.url))
            or (hit.raw_url != ORIGIN + path)
            or (hit.url != ORIGIN + path)
            or (hit.method != "POST")
            or (hit.status != 200)
            or hit.redirected
            or (not _json_type(hit.content_type))
            or (type(hit.req_body) is not dict)
            or (type(hit.resp_json) is not dict)
            or (type(hit.resp_json.get("resultCode")) is not str)
            or (hit.resp_json["resultCode"] != "0000")
            or (type(hit.resp_json.get("resultBody")) is not dict)
        ):
            return None
        observer = collector.observers.get(path)
        request = getattr(hit, "_native_request", None)
        records = (
            [] if observer is None else [r for r in observer.records.values() if r.get("native_request") is request]
        )
        if (
            observer is None
            or observer.bad
            or request is None
            or (len(records) != 1)
            or records[0]["bad"]
            or (not records[0]["used"])
            or (not records[0]["done"])
            or (not records[0]["response"])
            or (request.redirected_from is not None)
            or (request.redirected_to is not None)
        ):
            return None
        document = observer._document(hit.request_frame, hit.request_frame_url)
        if (
            document != (records[0]["key"][3], records[0]["loader"])
            or observer.bad
            or records[0]["bad"]
            or (collector.issued_count(path) != baseline["counts"][path] + 1)
            or (path in QUERY_PATHS and collector.query_issued_count() > collector.MAX_QUERY_REQUESTS)
            or (collector._latest_spa.get(path) != (sequence, hit))
        ):
            return None
        return hit

    hit = valid()
    if hit is None or valid() is not hit:
        raise ValueError("no current owned SPA success")
    return (hit.resp_json["resultBody"], hit.req_body)
