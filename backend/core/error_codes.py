"""Pure, registered action/reason identifiers; never serialize exception messages."""
from contextlib import ContextDecorator
from importlib import import_module

BANKS = frozenset({"cathay", "ctbc", "dbs", "esun", "fubon", "hsbc", "linebank", "rakuten", "scb", "scsb", "sinopac", "taishin", "ubot"})
# 900–999 actions are reserved for shared boundaries; bank leaves own 001–899.
COMMON_CODES = {
    "00-900-001": ("同步準備", "銀行模組載入失敗"),
    "00-910-001": ("同步準備", "憑證載入失敗"),
    "00-920-001": ("同步準備", "資料庫初始化失敗"),
    "00-930-001": ("同步準備", "讀取歷史游標失敗"),
    "01-900-001": ("登入頁面", "瀏覽器設定失敗"),
    "01-910-001": ("登入頁面", "瀏覽器啟動或導頁失敗"),
    "01-920-001": ("登入頁面", "回應觀察器建立失敗"),
    "01-930-001": ("登入頁面", "回應觀察器掛載失敗"),
    "01-940-001": ("登入頁面", "對話框處理器掛載失敗"),
    "01-950-001": ("登入頁面", "登入頁準備失敗"),
    "01-960-001": ("登入頁面來源驗證", "準備表單前的憑證來源檢查未通過"),
    "01-960-002": ("登入頁面來源驗證", "準備表單後的憑證來源檢查未通過"),
    "02-900-001": ("帳密輸入與送出", "尚未分類的帳密動作失敗"),
    "02-910-001": ("驗證碼辨識", "未取得可用辨識結果"),
    "03-900-001": ("登入結果", "尚未分類的登入失敗"),
    "03-910-001": ("登入結果", "登入方法未回報成功"),
    "03-920-001": ("登入檢查", "檢查器發生例外"),
    "03-930-001": ("登入檢查", "未知阻擋；未辨識具體原因"),
    "03-930-002": ("登入檢查", "需一次性密碼驗證"),
    "03-930-003": ("登入檢查", "需變更密碼"),
    "03-930-004": ("登入檢查", "原生登入錯誤已被辨識"),
    "03-940-001": ("登入檢查前", "原生JavaScript對話框阻擋"),
    "03-940-002": ("登入檢查前", "憑證來源檢查未通過"),
    "03-940-003": ("登入檢查後", "憑證來源檢查未通過"),
    "03-940-004": ("登入檢查後", "原生JavaScript對話框阻擋"),
    "03-950-001": ("登入檢查", "檢查規則名稱重複"),
    "03-950-002": ("登入檢查", "檢查規則銀行不相符"),
    "03-960-001": ("登入檢查", "檢查步數預算已耗盡"),
    "07-900-001": ("結果驗證", "回傳型別不符合合約"),
    "07-900-002": ("結果驗證", "回傳內容帶有失敗標記"),
    "07-900-003": ("結果驗證", "未宣告帳單事實完整性"),
    "07-910-001": ("結果序列化", "結果轉換失敗"),
    "07-920-001": ("結果發佈", "發佈證據檢查失敗"),
    "07-930-001": ("瀏覽器回呼", "瀏覽器回呼重複執行"),
    "07-940-001": ("瀏覽器收尾", "回應觀察器解除失敗"),
    "07-950-001": ("歷史覆蓋驗證", "查詢覆蓋證據未通過"),
    "07-960-001": ("銀行資料收集", "尚未分類的收集動作失敗"),
    "08-900-001": ("金融資料寫入", "尚未分類的寫入失敗"),
    "08-910-001": ("同步統計", "資料寫入後統計讀取失敗"),
    "08-920-001": ("資料庫收尾", "資料庫關閉失敗"),
    "09-900-001": ("同步工作", "尚未分類的工作失敗"),
    "09-910-001": ("同步排隊", "等待執行逾時"),
    "09-910-002": ("排程同步排隊", "等待執行逾時"),
    "09-920-001": ("同步執行", "執行逾時；結果未確認"),
}
COMMON_CODES.update({
    "03-940-005": ("驗證碼重載後", "原生JavaScript對話框阻擋"),
    "03-940-006": ("驗證碼重載後", "憑證來源檢查未通過"),
    "03-940-007": ("登入送出前", "原生JavaScript對話框阻擋"),
    "03-940-008": ("登入送出前", "憑證來源檢查未通過"),
    "03-940-009": ("玉山登入操作來源驗證", "來源檢查前的原生JavaScript對話框阻擋"),
    "03-940-010": ("玉山登入操作來源驗證", "憑證來源檢查未通過"),
    "03-940-011": ("玉山登入操作來源驗證", "來源檢查後的原生JavaScript對話框阻擋"),
    "07-970-001": ("收集來源驗證", "原生JavaScript對話框阻擋"),
    "07-970-002": ("收集來源驗證", "憑證來源檢查未通過"),
})
_FIELD = "_thoth_error_id"


def code_details(error_id, *, bank=None):
    """Return only a registered, bank-bound, source-owned description."""
    if type(error_id) is not str or len(error_id) > 128:
        return None
    if bank is not None and (type(bank) is not str or bank not in BANKS):
        return None
    prefix, separator, suffix = error_id.partition("-")
    owner = prefix.lower()
    if not separator or prefix != owner.upper() or owner not in BANKS | {"sys"}:
        return None
    if bank is not None and owner not in (bank, "sys"):
        return None
    details = COMMON_CODES.get(suffix)
    if details is None:
        stage = suffix.partition("-")[0]
        if stage == "07" and suffix[3:6] in {str(n) for n in range(980, 990)}:
            module = "validation"
            catalog_id = f"SYS-{suffix}"
        elif stage == "03" and suffix[3:6] in {str(n) for n in range(970, 990)}:
            module = "checkpoints"
            catalog_id = f"SYS-{suffix}"
        else:
            module = "persist" if stage == "08" else "system" if stage in ("00", "09") or owner == "sys" else owner
            catalog_id = error_id
        try:
            catalog = import_module(f"backend.core.error_catalog.{module}").CODES
        except (ImportError, AttributeError):
            return None
        if type(catalog) is not dict or len(catalog) > 4096:
            return None
        if any(type(key) is not str for key in dict.keys(catalog)):
            return None
        details = dict.get(catalog, catalog_id)
    if type(details) is not tuple or len(details) != 2 or any(
        type(item) is not str or not item or len(item) > 256 for item in details
    ):
        return None
    return details


def _exception_state(error):
    try:
        state = BaseException.__dict__["__dict__"].__get__(error, BaseException)
    except BaseException:
        return None
    if type(state) is not dict or len(state) > 64 or any(type(key) is not str for key in dict.keys(state)):
        return None
    return state


def with_error_code(error, error_id):
    """Tag the same exception, keeping its type, traceback and earliest leaf code."""
    if code_details(error_id) is None:
        return error
    state = _exception_state(error)
    if state is not None and code_details(dict.get(state, _FIELD)) is None:
        dict.__setitem__(state, _FIELD, error_id)
    return error


class error_step(ContextDecorator):
    """Tag an action without the generator exit's normal traceback assignment."""

    def __init__(self, error_id):
        self.error_id = error_id

    def __enter__(self):
        return None

    def __exit__(self, error_type, error, traceback):
        if error_type is not None and issubclass(error_type, Exception):
            with_error_code(error, self.error_id)
        return False


def exception_error_id(error, *, bank=None):
    """Prefer explicit causes; use implicit context only without a registered ID."""
    pending = [error]
    seen = set()
    found = None
    while pending and len(seen) < 16:
        current = pending.pop()
        if id(current) in seen or not issubclass(type(current), BaseException):
            continue
        seen.add(id(current))
        state = _exception_state(current)
        candidate = dict.get(state, _FIELD) if state is not None else None
        if code_details(candidate, bank=bank) is not None:
            found = candidate
        for field in ("__cause__", "__context__"):
            if field == "__context__" and found is not None:
                break  # A handled polling failure is not an explicit cause.
            try:
                nested = BaseException.__dict__[field].__get__(current, BaseException)
            except BaseException:
                continue
            if nested is not None:
                pending.append(nested)
                break  # An explicit cause is primary; from-None still retains context.
    return found


def format_error_id(error_id, *, bank=None):
    details = code_details(error_id, bank=bank)
    if details is None:
        return None
    action, reason = details
    return f"[{error_id}] {action}：{reason}"


def bank_bound_error_id(bank, error_id):
    """Qualify SYS only if the bank catalog preserves its action and reason."""
    details = code_details(error_id, bank=bank)
    if details is None:
        return None
    if error_id.startswith("SYS-"):
        qualified = bank_error_id(bank, error_id[4:])
        if qualified is not None and code_details(qualified, bank=bank) == details:
            return qualified
    return error_id


def bank_error_id(bank, suffix):
    """Qualify a source-owned shared suffix using an exact supported bank."""
    if type(bank) is not str or bank not in BANKS or type(suffix) is not str:
        return None
    error_id = f"{bank.upper()}-{suffix}"
    return error_id if code_details(error_id, bank=bank) is not None else None
