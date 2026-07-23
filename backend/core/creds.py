#!/usr/bin/env python3
"""Bank login credential loader (unified base + thin per-bank subclasses).

銀行登入憑證讀取（統一基類 + 子類薄殼）。

Phase 2 後：load() 只剩 DB → env → raise 三層 fallback，Bitwarden CLI 邏輯整層砍除。

設計：所有銀行共用 BankCreds 基類完成「DB / env / .env」三路 fallback。
子類只需宣告 2 樣：
  - BANK: ClassVar[str]    — env 前綴（如 "SINOPAC"）
  - dataclass field        — 憑證欄位（national_id/user_code/password 等）

env 變數命名自動為 <BANK>_<ATTR>（大寫），如 SINOPAC_NATIONAL_ID，無需手寫 from_env。

來源優先順序（load 內共用邏輯）：
  1. DB-backed（server-mode，由 env `BANK_CRAWLER_USER_ID` 啟用）
  2. <BANK>_<FIELD> 環境變數（含 .env，自動載入專案根目錄）
  3. 缺欄位 → raise CredError

設計規範：明文不落程式碼、不入 git；.env 在 .gitignore 內。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, fields as dc_fields
from pathlib import Path
from typing import ClassVar


class CredError(RuntimeError):
    pass


# ============================================================
# .env 載入（極簡實作，無外部 dep）
# ============================================================

_ENV_LOADED = False


def _load_dotenv() -> None:
    """載入 .env 進 os.environ（一次性、不覆蓋已存在的環境變數）。

    2026-06-14 拆分後三層 .env (見 wiki: thoth-env-three-layer-split-lesson):
      1. backend/server/.env  — server runtime secrets (JWT_SECRET / SERVER_FERNET_KEY / ...)
      2. cli/.env             — CLI/MCP/probe bank credentials (CATHAY_* / CTBC_* / ...)
      3. (legacy) .env        — 過渡期保留 fallback,使用者手動清掉後此分支即無作用

    載入順序 = server → cli → root(legacy)
    override=False 所以先載的 var 不會被後載覆蓋,shell export 永遠最大。
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    # backend/core/creds.py → parents[0]=core, parents[1]=backend, parents[2]=專案根
    root = Path(__file__).resolve().parents[2]
    candidates = [
        root / "backend" / "server" / ".env",  # server secrets
        root / "cli" / ".env",                 # CLI/probe bank creds
        root / ".env",                         # legacy fallback (transitional)
    ]
    for cand in candidates:
        if cand.exists():
            _load_one_env(cand)
    return


def _load_one_env(env_path: Path) -> None:
    """載一個 .env 檔到 os.environ (override=False, shell export 永遠最大)."""
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception as e:
        print(f"[creds] .env 載入失敗 {env_path}: {e}", file=sys.stderr)


def _from_env(name: str) -> str | None:
    """讀環境變數（會先載入 .env）。"""
    _load_dotenv()
    return os.environ.get(name)


# ============================================================
# BankCreds 基類（所有銀行共用三路 fallback）
# ============================================================

@dataclass
class BankCreds:
    """銀行登入憑證基類——子類只需宣告 BANK ClassVar 與 dataclass field。

    env var 命名規範：`<BANK>_<ATTR>`（大寫）→ 自動從 dataclass field 推導。
    """

    BANK: ClassVar[str] = ""           # env 前綴（如 "SINOPAC"）

    # ─────────────────────────────────────────────────────────
    # Placeholder guard（2026-06-13 LINE Bank 三連錯後加上的鐵律）
    # ─────────────────────────────────────────────────────────
    # 任何 env / DB / shell 來源的值若命中下列 pattern，立刻 raise，**絕不送銀行**。
    # 起因：在 LINE Bank live login 連續 3 次把 shell command 裡的 '***' 當真
    # 密碼送出，導致使用者帳號被吃掉嘗試次數。Step 2.5 長度檢查無效（target 自身
    # 就是 placeholder 長度，比對通過）。這層 guard 從根源擋掉。
    _PLACEHOLDER_LITERALS: ClassVar[tuple[str, ...]] = (
        "***", "****", "*****", "******",
        "xxx", "xxxx", "yyy", "yyyy",
        "redacted", "REDACTED", "TODO", "FIXME",
        "placeholder", "PLACEHOLDER",
        "your-password", "your_password",
        "changeme", "change-me", "change_me",
        "dummy", "test", "fake",
    )

    @classmethod
    def _attrs(cls) -> list[str]:
        """回傳本 class 的所有 dataclass field 名（憑證欄位順序）。"""
        return [f.name for f in dc_fields(cls)]

    def _check_no_placeholder(self) -> None:
        """守衛：任何 cred 欄位若是 placeholder/全 * 字串/過短，立刻 raise。

        檢查規則（任一觸發即 fail）：
          (1) 字面命中 _PLACEHOLDER_LITERALS（不分大小寫）
          (2) 整串都是同一個字元（如 '***', '****', 'aaaaa'）且長度 <= 8
          (3) password 欄位長度 < 6（銀行最短 6 碼，更短一定是 placeholder）

        對非密碼欄位（national_id / user_id / user_code）僅做 (1)(2) 檢查，
        因 user_code 等可能短至 4 碼（CTBC 早期）。
        """
        for attr in self._attrs():
            v = getattr(self, attr, "") or ""
            if not isinstance(v, str):
                continue
            v_stripped = v.strip()
            if not v_stripped:
                continue
            # (1) 字面 placeholder
            if v_stripped.lower() in (p.lower() for p in self._PLACEHOLDER_LITERALS):
                raise CredError(
                    f"[{self.BANK}.{attr}] 是 placeholder 字串 {v_stripped!r}, 拒絕送出。"
                    f"請設真實憑證 (env: {self.BANK}_{attr.upper()})。",
                )
            # (2) 同字元 spam（如 '***', '****', 'aaaaa'）
            if len(v_stripped) <= 8 and len(set(v_stripped)) == 1:
                raise CredError(
                    f"[{self.BANK}.{attr}] 整串都是同一字元 {v_stripped!r}, 拒絕送出。"
                    f"看起來是 placeholder。",
                )
            # (3) password 過短
            if "password" in attr.lower() and len(v_stripped) < 6:
                raise CredError(
                    f"[{self.BANK}.{attr}] 密碼長度 {len(v_stripped)} < 6, 拒絕送出。"
                    f"銀行密碼最短 6 碼，更短一定是 placeholder。",
                )

    @classmethod
    def from_env(cls):
        """全部從環境變數讀（含 .env）。

        嚴格分層（(2026-06-12) 拍板）：本 path 只給 **CLI / MCP / probe / unit
        test** 用，不給 server-mode (web/iOS)。`load()` 在 server-mode 不會 fall
        through 到這裡（強制 raise）；server-mode 的 cred 一律是「每個 user 在
        Settings UI 自己填的 DB Fernet」，避免拿 maintainer 本人 .env 跑別 user 的爬蟲。
        """
        _load_dotenv()
        vals = {a: _from_env(f"{cls.BANK}_{a.upper()}") for a in cls._attrs()}
        missing = [f"{cls.BANK}_{k.upper()}" for k, v in vals.items() if not v]
        if missing:
            raise CredError(f"env: {cls.BANK} 缺少 {missing}")
        inst = cls(**vals)
        inst._check_no_placeholder()
        return inst

    @classmethod
    def from_db(cls, user_id: int):
        """[DEPRECATED L5-1] Server-mode：從 backend.server DB 取 Fernet 解密後的所有欄位。

        舊 single-account 模式: 直接用 (user_id, bank) 撈 cred。L5-1 起改用
        `from_account(account_id)` (新表 bank_credentials_v2)。

        這個 method 仍會跑——但只回最舊版 bank_credentials 表的 row。
        如果 user 在 Settings UI 有建多個 account, 這條路會撈不到
        (新 cred 寫到 v2 表)。caller 已全部改 from_account 即可。

        缺欄位 → raise CredError（嚴格模式下 caller `load()` 不會 catch、直接 raise）。
        """
        # 延遲 import：避免在沒裝 cryptography / 沒走 server-mode 時 import 失敗。
        from backend.server.creds_store import LocalFernetBackend

        backend = LocalFernetBackend()
        vals = backend.get_all_for_bank(user_id=user_id, bank=cls.BANK.lower())
        missing = [a for a in cls._attrs() if not vals.get(a)]
        if missing:
            raise CredError(f"db: user_id={user_id} bank={cls.BANK.lower()} 缺少 {missing}")
        inst = cls(**{a: vals[a] for a in cls._attrs()})
        inst._check_no_placeholder()
        return inst

    @classmethod
    def from_account(cls, account_id: int, *, expected_owner_user_id: int | None = None):
        """[L5-1] Server-mode：以 account_id 載 Fernet 解密欄位。

        account_id 由 bank_accounts 表 PK；和 cls.BANK 不需吻合驗證
        (caller 已知 account 屬於哪間銀行；若不吻合是 caller bug)。
        缺欄位 → raise CredError。

        Defense-in-depth (Phase C-Suggestion 2026-06-17):
        傳 expected_owner_user_id 時, LocalFernetBackend 會 verify
        bank_accounts.user_id 對得上才 decrypt; 不符直接 raise PermissionError。
        sync_runner 從 sync_jobs.user_id 拿 owner, 應永遠傳。
        """
        from backend.server.creds_store import LocalFernetBackend

        backend = LocalFernetBackend()
        vals = backend.get_all_for_account(
            account_id=account_id,
            expected_owner_user_id=expected_owner_user_id,
        )
        missing = [a for a in cls._attrs() if not vals.get(a)]
        if missing:
            raise CredError(
                f"db: account_id={account_id} bank={cls.BANK.lower()} 缺少 {missing}",
            )
        inst = cls(**{a: vals[a] for a in cls._attrs()})
        inst._check_no_placeholder()
        return inst

    @classmethod
    def load(cls):
        """嚴格分層 cred 載入（(2026-06-12) 拍板，不再 fall through）：

        L5-1 起三種模式 (優先序由上而下)：

        - `BANK_CRAWLER_ACCOUNT_ID` 設了 → **account-mode**：用 account_id 從 v2 表載；
          缺就 raise。這是 L5-1 後 server-mode 標準路徑。
        - `BANK_CRAWLER_USER_ID` 設了 → **legacy server-mode**：用 (user, bank) 從 v1 表載；
          缺就 raise。保留給尚未升級的 callers。L5-end 砍。
        - 兩者皆未設 → **CLI/MCP-mode**：走 env (含 .env)；
          缺欄位由 from_env() raise。
        """
        account_id_raw = os.environ.get("BANK_CRAWLER_ACCOUNT_ID")
        if account_id_raw:
            # Defense-in-depth (Phase C-Suggestion 2026-06-17):
            # sync_runner 同 thread 一定設 BANK_CRAWLER_USER_ID, 拿來餵 from_account
            # owner check; account 不屬此 user → PermissionError.
            user_id_for_check = os.environ.get("BANK_CRAWLER_USER_ID")
            expected_owner = int(user_id_for_check) if user_id_for_check else None
            return cls.from_account(
                int(account_id_raw),
                expected_owner_user_id=expected_owner,
            )
        user_id_raw = os.environ.get("BANK_CRAWLER_USER_ID")
        if user_id_raw:
            # Legacy: 老 caller 還沒升; v1 表 row 在 L5-1 migration 後也存活
            return cls.from_db(int(user_id_raw))
        # CLI / MCP / probe / unit test：走環境變數
        return cls.from_env()


# ============================================================
# 各家銀行子類（每家只 ~5 行宣告）
# ============================================================

@dataclass
class CtbcCreds(BankCreds):
    """中信。env: CTBC_NATIONAL_ID / CTBC_USER_CODE / CTBC_PASSWORD"""
    BANK: ClassVar[str] = "CTBC"
    national_id: str = ""
    user_code: str = ""
    password: str = ""


@dataclass
class CathayCreds(BankCreds):
    """國泰世華 MyBank。env: CATHAY_CUST_ID / CATHAY_USER_ID / CATHAY_PASSWORD"""
    BANK: ClassVar[str] = "CATHAY"
    cust_id: str = ""
    user_id: str = ""
    password: str = ""


@dataclass
class HsbcCreds(BankCreds):
    """匯豐信用卡網銀（兩段式）。env: HSBC_USER_ID / HSBC_PASSWORD"""
    BANK: ClassVar[str] = "HSBC"
    user_id: str = ""
    password: str = ""


@dataclass
class ScsbCreds(BankCreds):
    """上海商業儲蓄銀行 iBank。env: SCSB_NATIONAL_ID / SCSB_USER_CODE / SCSB_PASSWORD"""
    BANK: ClassVar[str] = "SCSB"
    national_id: str = ""
    user_code: str = ""
    password: str = ""


@dataclass
class SinopacCreds(BankCreds):
    """永豐 MMA。env: SINOPAC_NATIONAL_ID / SINOPAC_USER_CODE / SINOPAC_PASSWORD"""
    BANK: ClassVar[str] = "SINOPAC"
    national_id: str = ""
    user_code: str = ""
    password: str = ""


@dataclass
class UbotCreds(BankCreds):
    """聯邦銀行。env: UBOT_NATIONAL_ID / UBOT_USER_CODE / UBOT_PASSWORD"""
    BANK: ClassVar[str] = "UBOT"
    national_id: str = ""
    user_code: str = ""
    password: str = ""


@dataclass
class EsunCreds(BankCreds):
    """玉山銀行 ebank.esunbank.com.tw。env: ESUN_NATIONAL_ID / ESUN_USER_CODE / ESUN_PASSWORD"""
    BANK: ClassVar[str] = "ESUN"
    national_id: str = ""
    user_code: str = ""
    password: str = ""


@dataclass
class TaishinCreds(BankCreds):
    """台新銀行 my.taishinbank.com.tw。env: TAISHIN_NATIONAL_ID / TAISHIN_USER_CODE / TAISHIN_PASSWORD"""
    BANK: ClassVar[str] = "TAISHIN"
    national_id: str = ""
    user_code: str = ""
    password: str = ""


@dataclass
class TaipeiFubonCreds(BankCreds):
    """台北富邦 ebank.taipeifubon.com.tw。env: FUBON_NATIONAL_ID / FUBON_USER_CODE / FUBON_PASSWORD"""
    BANK: ClassVar[str] = "FUBON"
    national_id: str = ""
    user_code: str = ""
    password: str = ""


@dataclass
class DbsCreds(BankCreds):
    """星展（台灣）internet-banking.dbs.com.tw/digitw/。
    env: DBS_USERNAME / DBS_PASSWORD（兩欄即可，無身分證、無 captcha）"""
    BANK: ClassVar[str] = "DBS"
    username: str = ""
    password: str = ""


@dataclass
class ScbCreds(BankCreds):
    """台灣渣打 ebank.standardchartered.com.tw/scb/。
    env: SCB_NATIONAL_ID / SCB_USERNAME / SCB_PASSWORD（3 欄帳密 + captcha）"""
    BANK: ClassVar[str] = "SCB"
    national_id: str = ""
    username: str = ""
    password: str = ""


@dataclass
class LinebankCreds(BankCreds):
    """LINE Bank 連線商業銀行 accessibility.linebank.com.tw/login。
    env: LINEBANK_NATIONAL_ID / LINEBANK_USER_CODE / LINEBANK_PASSWORD

    欄位順序與 form name 對應：
      身分證字號 #nationalId  → national_id (maxLength 10)
      使用者代號 #userId      → user_code   (maxLength 14)
      密碼      #pw          → password    (maxLength 14)

    無 CAPTCHA、無裝置綁定提示（送出後可能跳 OTP / 簡訊驗證碼，登入流程裡再處理）。
    """
    BANK: ClassVar[str] = "LINEBANK"
    national_id: str = ""
    user_code: str = ""
    password: str = ""


# ============================================================
# 註冊表：給 CLI 列舉用
# ============================================================

ALL_CREDS = [
    CathayCreds, UbotCreds, HsbcCreds, CtbcCreds, ScsbCreds, SinopacCreds,
    EsunCreds, TaishinCreds, TaipeiFubonCreds, DbsCreds, ScbCreds,
    LinebankCreds,
]
