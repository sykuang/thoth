"""E.SUN SPA card presence and native IESC bill read, RAM-only and incomplete."""
from collections.abc import Callable
from datetime import date
from time import monotonic
from urllib.parse import urlsplit

from backend.core.base import BankCollectResult, _HistoryBodyObserver, _OriginGuardProxy
from .capture import PATHS, LIMIT, _json, _json_type, require_current_success
from .collection import navigation_guard
from .navigation import build_menu_plan, navigate_twd, TIMEOUT

DESTINATION = '''() => {
 const layouts=[...document.querySelectorAll('#layout-content')];
 const owners=[...document.querySelectorAll('#layout-content [mfe-service="esb/card"]')];
 const roots=[...document.querySelectorAll('#layout-content .ccm01002')];
 const visible=n=>{for(let p=n;p;p=p.parentElement)if(p.hidden||p.inert||p.getAttribute('aria-hidden')==='true'||getComputedStyle(p).visibility!=='visible')return false;return !!n.getClientRects().length};
 return location.pathname==='/esb/card/credit/overview' && !location.search && !location.hash &&
 layouts.length===1 && !layouts[0].classList.contains('not-login') && owners.length===1 && roots.length===1 &&
 owners[0].contains(roots[0]) && visible(roots[0]) && roots[0].children.length===1 &&
 roots[0].firstElementChild.tagName==='DIV' && visible(roots[0].firstElementChild);
}'''
BILL_BUTTON = '#layout-content [mfe-service="esb/card"] .ccm01002 .footer-double-btn-container > div.footer-double-item.right[role="button"]'
HOSTS = frozenset(('ebank.esunbank.com.tw', 'iesc.esunbank.com'))
BILL_URLS = {
    'summary': 'https://iesc.esunbank.com/GW/creditBill/getSummaryResult',
    'detail': 'https://iesc.esunbank.com/GW/creditBill/getDetailResult',
}
UNPOSTED_BUTTON = '#layout-content [mfe-service="esb/card"] .ccm01002 :text-is("刷卡明細")'
UNPOSTED_URLS = {
    'summary': 'https://iesc.esunbank.com/GW/creditUnposted/getSummaryResult',
    'detail': 'https://iesc.esunbank.com/GW/creditUnposted/getDetailResult',
}
BILL_TOTAL = 2 * LIMIT


def _statement_cycle(body):
    """Latest TWD statement (close date, due date, total). Never a remaining-due fact."""
    info = body.get('billInfo') if type(body) is dict else None
    if type(info) is not dict:
        return None
    totals = info.get('billTotalInfoList')
    if type(totals) is not list or len(totals) != 1 or type(totals[0]) is not dict:
        return None  # ponytail: TWD-only bills; foreign-currency statements stay unavailable
    total = totals[0]
    amount = total.get('billTotalAmount')
    if total.get('billTotalCurrency') != 'TWD' or type(amount) is not int or not 0 <= amount <= 100_000_000:
        return None
    dates = []
    for key in ('billDate', 'paymentDueDate'):
        raw = info.get(key)
        if type(raw) is not str or len(raw) != 8 or not raw.isdigit():
            return None
        try:
            dates.append(date(int(raw[:4]), int(raw[4:6]), int(raw[6:])).isoformat())
        except ValueError:
            return None
    if dates[0] > dates[1]:
        return None
    return {'statement_close_date': dates[0], 'payment_due_date': dates[1], 'statement_amount': amount}


def _statement_transactions(body, bill_date):
    """Statement detail rows → billed card_transactions. None when any row is malformed."""
    groups = body.get('transList') if type(body) is dict else None
    if type(groups) is not list or type(bill_date) is not str or len(bill_date) != 10:
        return None
    close_year, close_mmdd = int(bill_date[:4]), bill_date[5:7] + bill_date[8:]

    def iso(mmdd):  # statement rows never post after the close date; later MMDD is last year
        return f'{close_year - (mmdd > close_mmdd)}-{mmdd[:2]}-{mmdd[2:]}'

    rows = []
    for group in groups:
        for t in (group.get('transDetailList') if type(group) is dict else None) or []:
            trans, post, card = (t.get(k) for k in ('transMonthDay', 'postingMonthDay', 'cardNo'))
            amount = t.get('paymentAmount')
            if (type(amount) is not int or type(card) is not str or not card[-4:].isdigit()
                    or not all(type(v) is str and len(v) == 4 and v.isdigit() for v in (trans, post))):
                return None
            foreign = t.get('transCurrency') not in (None, '', 'TWD')
            rows.append({
                'card_last4': card[-4:],
                'consume_date': iso(trans),
                'post_date': iso(post),
                'merchant': (t.get('merchantName') or '').strip(),
                'billed_amount': amount,
                'billed_currency': t.get('paymentCurrency') or 'TWD',
                'consume_currency': t.get('transCurrency') if foreign else None,
                'consume_amount': t.get('transAmount') if foreign else None,
                'status': '已入帳',
                'bill_month': bill_date[:7],
            })
    return rows


def _unposted_transactions(body):
    """Unposted (未出帳) detail rows → pending card_transactions. None when any row is malformed."""
    groups = body.get('transList') if type(body) is dict else None
    if type(groups) is not list or body.get('rtnCode') not in (None, '0000'):
        return None
    rows = []
    for group in groups:
        year = group.get('year') if type(group) is dict else None
        if type(year) is not str or len(year) != 4 or not year.isdigit():
            return None
        for t in group.get('transDetailList') or []:
            trans, card, amount = t.get('transMonthDay'), t.get('cardNo'), t.get('paymentAmount')
            if (type(amount) is not int or type(card) is not str or not card[-4:].isdigit()
                    or type(trans) is not str or len(trans) != 4 or not trans.isdigit()):
                return None
            foreign = t.get('transCurrency') not in (None, '', 'TWD')
            rows.append({
                'card_last4': card[-4:],
                'consume_date': f'{year}-{trans[:2]}-{trans[2:]}',
                'post_date': None,
                'merchant': (t.get('merchantName') or '').strip(),
                'billed_amount': amount,
                'billed_currency': t.get('paymentCurrency') or 'TWD',
                'consume_currency': t.get('transCurrency') if foreign else None,
                'consume_amount': t.get('transAmount') if foreign else None,
                'status': '未入帳',
            })
    return rows


def _cleanup_all(actions):
    pending = list(actions)
    for _ in range(2):
        retry = []
        for action in pending:
            try:
                action()
            except Exception:
                retry.append(action)
        pending = retry
        if not pending:
            return
    raise RuntimeError('E.SUN native bill cleanup incomplete') from None


def _official(url):
    try:
        u = urlsplit(url)
        return u.scheme == 'https' and u.hostname in HOSTS and u.port in (None, 443) and not u.username and not u.password
    except ValueError:
        return False


def _project_bill(kind, body):
    """Only shape/count evidence; never publish statement amounts without account coverage."""
    if type(body) is not dict:
        return None
    if kind == 'summary':
        info = body.get('billInfo')
        if type(info) is not dict or type(info.get('billTotalInfoList')) is not list or len(info['billTotalInfoList']) > 100:
            return None
        payments = body.get('paymentInfoList')
        if type(payments) is not list or len(payments) > 100:
            return None
        return {'shape': kind, 'totals': len(info['billTotalInfoList']), 'payments': len(payments)}
    if kind == 'detail':
        groups = body.get('transList')
        if type(groups) is not list or len(groups) > 120 or any(type(g) is not dict or type(g.get('transDetailList')) is not list or len(g['transDetailList']) > 1000 for g in groups):
            return None
        if type(body.get('cardInfoList')) is not list or type(body.get('currencyInfoList')) is not list:
            return None
        return {'shape': kind, 'groups': len(groups), 'rows': sum(len(g['transDetailList']) for g in groups)}
    return None


def _overview(crawler, page, collector, login_baseline):
    def require(ok):
        if not ok:
            raise ValueError('SPA card overview rejected')

    crawler._esun_spa_phase = 'card_navigation'
    require(collector.page is _OriginGuardProxy._unwrap(page))
    guard = navigation_guard(crawler, page)

    def current_plan():
        guard()
        body, envelope = require_current_success(collector, login_baseline, PATHS[0])
        request = envelope.get('requestBody')
        require(type(request) is dict)
        return build_menu_plan(body.get('menuList'), request.get('locale'), target='CCM01002')

    plan = current_plan()

    def revalidate():
        require(current_plan() == plan)
        return plan

    baseline = collector.snapshot()
    navigate_twd(page, plan, guard, revalidate, target='CCM01002')
    crawler._esun_spa_phase = 'card_overview'
    deadline = monotonic() + TIMEOUT / 1000
    while monotonic() < deadline:
        revalidate()
        try:
            body, request = require_current_success(collector, baseline, PATHS[4])
        except ValueError:
            page.wait_for_timeout(20)
            continue
        # The child only mounts after native init succeeds; its own owned
        # overview response plus destination is the read proof. Init's response
        # body need not satisfy our separate generic SPA JSON-body contract.
        require(collector.issued_count(PATHS[3]) == baseline['counts'][PATHS[3]] + 1)
        require(request.get('requestBody') == {})
        require(collector._latest_spa[PATHS[3]][0] < collector._latest_spa[PATHS[4]][0])
        require(type(body.get('haveCreditCard')) is bool)
        if page.evaluate(DESTINATION) is True:
            break
        page.wait_for_timeout(20)
    else:
        raise ValueError('SPA card overview timeout')

    def bound():
        revalidate()
        require(collector.issued_count(PATHS[3]) == baseline['counts'][PATHS[3]] + 1)
        require(collector._latest_spa[PATHS[3]][0] < collector._latest_spa[PATHS[4]][0])
        current, _ = require_current_success(collector, baseline, PATHS[4])
        require(current is body and page.evaluate(DESTINATION) is True)

    bound()
    return body['haveCreditCard'], bound


def _open_bill(page, bound, selector=BILL_BUTTON, urls=BILL_URLS):
    """One actual native click; observe only popup-owned official bill responses."""
    native = _OriginGuardProxy._unwrap(page)
    context = native.context
    bound()
    button = page.locator(selector)
    if button.count() != 1 or not button.is_visible() or not button.is_enabled():
        raise ValueError('SPA bill button rejected')
    receipt: dict[str, object] = {'popup': False, 'summary': False, 'detail': False}
    cycles: dict[str, object] = {}
    popup = None
    observers = {}
    admitted = 0

    def reserve(size):
        nonlocal admitted
        if type(size) is not int or not 0 <= size <= LIMIT or admitted + size > BILL_TOTAL:
            return False
        admitted += size
        return True

    def route_request(route):
        # Context routing covers popup redirects too; never follow foreign hosts.
        if _official(route.request.url):
            route.continue_()
        else:
            route.abort()

    def response_seen(response):
        kind = next((kind for kind, url in urls.items() if response.url == url), None)
        if popup is None or kind is None or response.status != 200 or kind not in observers:
            return
        try:
            req = response.request
            if (req.frame is not popup.main_frame or req.frame.page is not popup
                    or not _json_type(response.headers.get('content-type', ''))):
                return
            raw = observers[kind].read(response, req.frame, req.frame.url,
                                       lambda: BILL_TOTAL - admitted, 1, reserve)
            if raw is None or len(raw) > LIMIT:
                return
            payload = _json(raw)
            if type(payload) is not dict or payload.get('status') != '200':
                return
            projection = _project_bill(kind, payload.get('body')) if urls is BILL_URLS else {'shape': kind}
            if projection is not None:
                receipt[kind] = projection
                if kind == 'summary':
                    cycles['summary'] = _statement_cycle(payload.get('body')) if urls is BILL_URLS else None
                else:
                    cycles['detail'] = payload.get('body')
        except Exception:
            pass  # An unavailable bounded observation never becomes a fact.

    route_installed = False
    response_installed = False
    try:
        context.route('**/*', route_request)
        route_installed = True
        context.on('response', response_seen)
        response_installed = True
        bound()
        with native.expect_popup(timeout=5000) as opened:
            button.click(timeout=TIMEOUT, no_wait_after=True)
        popup = opened.value
        if popup.opener() is not native or popup.context is not context:
            raise ValueError('SPA bill popup owner rejected')
        for kind, url in urls.items():
            observer = _HistoryBodyObserver(popup, url)
            observer.LIMIT, observer.TOTAL_LIMIT, observer.MAX_RECORDS = LIMIT, BILL_TOTAL, 16
            observer.WAIT_SECONDS = 1  # Optional diagnostics must not stall the main read.
            observers[kind] = observer
            observer.start()
        deadline = monotonic() + 7
        while monotonic() < deadline:
            if _official(popup.url) and urlsplit(popup.url).hostname == 'iesc.esunbank.com' and urlsplit(popup.url).path.startswith('/IESC/'):
                receipt['popup'] = True
                break
            native.wait_for_timeout(50)
        if receipt['popup']:
            while monotonic() < deadline and not (receipt['summary'] and receipt['detail']):
                native.wait_for_timeout(50)
        # Amounts stay out of telemetry; the caller pops this private key.
        receipt['_statement_cycle'] = cycles.get('summary')
        receipt['_detail'] = cycles.get('detail')
        return receipt
    finally:
        actions: list[Callable[[], object]] = [
            lambda observer=observer: observer.close() for observer in observers.values()
        ]
        if response_installed:
            actions.append(lambda: context.remove_listener('response', response_seen))
        if route_installed:
            actions.append(lambda: context.unroute('**/*', route_request))
        _cleanup_all(actions)


def collect_products(crawler, page, collector, login_baseline):
    """Read product presence and optional native bill; never certify sync success."""
    presence, bound = _overview(crawler, page, collector, login_baseline)
    evidence = {'card_presence': presence, 'popup': False, 'summary': False, 'detail': False}
    cycle = txns = pending = None
    if presence:
        crawler._esun_spa_phase = 'native_bill'
        try:
            evidence.update(_open_bill(page, bound))
            cycle = evidence.pop('_statement_cycle', None)
            detail = evidence.pop('_detail', None)
            if cycle:
                txns = _statement_transactions(detail, cycle['statement_close_date'])
        except Exception:
            # A failed native handoff is an incomplete read, never a bill fact.
            evidence['native_bill_unavailable'] = True
        crawler._esun_spa_phase = 'native_unposted'
        try:
            receipt = _open_bill(page, bound, UNPOSTED_BUTTON, UNPOSTED_URLS)
            pending = _unposted_transactions(receipt.get('_detail'))
            evidence['unposted'] = {k: receipt.get(k) is not False for k in ('popup', 'summary', 'detail')}
        except Exception as exc:
            evidence['native_unposted_unavailable'] = type(exc).__name__
    evidence['statement_cycle'] = cycle is not None
    evidence['statement_txns'] = None if txns is None else len(txns)
    evidence['unposted_txns'] = None if pending is None else len(pending)
    # Unposted read is authoritative only when the full detail parsed; else keep DB pending untouched.
    if pending is not None:
        txns = (txns or []) + pending
    print(f"[esun][spa-products] {evidence}", flush=True)  # shapes/counts only, never amounts
    return BankCollectResult(bank='esun', error='spa_collection_incomplete', card_bill_facts_ok=False,
                             card_statement_cycle=cycle, card_transactions=txns,
                             card_transactions_ok=pending is not None,
                             telemetry={'esun_spa_products': evidence})
