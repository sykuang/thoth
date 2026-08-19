# Shared Bank Login Checkpoints Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 把公告通知、啟動修復、重複登入、OTP、密碼變更與登入成功判定收斂成共用 login checkpoint state machine；銀行 adapter 只負責頁面準備、一次性送出帳密與 declarative gate rules。

**Architecture:** `BankCrawler.run()` 擁有完整 orchestration：`initial page → pre-submit checkpoint → one credential submission → post-submit checkpoint loop → collect → logout`。`backend/core/login_checkpoints.py` 統一偵測 gate、選擇安全 action、限制 reload／credential submission／protocol resubmit budget，並回傳 typed outcome；不再讓每家銀行的 monolithic `login()` 自己猜 OTP、踢 session、關通知或判斷密碼變更。

**Tech Stack:** Python 3.12、`dataclass`／`StrEnum`、Playwright/Patchright Locator、pytest、Ruff。

---

## Git history evidence

| Bank / commit | Gate | Existing behavior | Shared checkpoint requirement |
|---|---|---|---|
| Cathay `ddfda1323a5a` | pre-submit notice | `#divSystemLoginMsgList` 最多 12 輪，最後 force-hide modals | scoped multi-page notice rule；禁止 DOM surgery |
| CTBC `9b5d8125353e` | pre-submit notice | `重要公告` + `.btn_close`，再驗登入欄 | body marker + structural close rule |
| Rakuten `be254743eec3` | startup recovery | `#ib_init_connect_error_popup` 最多 reload 一次 | shared `RELOAD_ONCE` action，不消耗 credential budget |
| Rakuten `d1b8e5b807c4`／SCB current source／E.SUN current source | duplicate session | 各家自行找 modal、確認、等待 | shared `CONFIRM_SESSION` gate，銀行只宣告 matcher/action |
| Taishin current source | duplicate session requiring protocol resubmit | 明確 popup 後再次送出帳密 | shared `PROTOCOL_RESUBMIT` transition；只有 explicit rule 可把 max submission 從 1 提到 2 |
| Rakuten current source／DBS／E.SUN docs | OTP / device verification | 有的 raise typed exception，有的只把 OTP 當「登入進行中」 | shared `INTERACTION_REQUIRED(otp)` terminal outcome；不自動填、不重送 |
| UBot `814f28b4b6b9` | optional password-change reminder | 將 nag 視為已登入，再以寬 regex 點掉 | shared `PASSWORD_CHANGE_OPTIONAL` dismiss rule |
| Mandatory password-change pages | mandatory password change | 現行多半混入 generic login failure | shared `INTERACTION_REQUIRED(password_change)`；不得自動改密碼 |
| LINE Bank／UBot／Taishin／Rakuten | post-submit informational notice | 各自 page-global click／force-hide | shared `DISMISSIBLE_NOTICE` rules |

## Correct lifecycle model

`startup recovery` 可能出現在送出前，`duplicate session／OTP／password change／informational notice` 通常出現在送出後；它們都屬於同一個 **login checkpoint layer**，只是 phase 不同。

```text
StealthyFetcher page ready
  ↓
prepare_login_page()             # hydrate / open login modal; no credentials
  ↓
checkpoint(PRE_SUBMIT)           # notices + startup recovery + existing session
  ├─ AUTHENTICATED ───────────────→ checkpoint(POST_SUBMIT_SETTLE)
  └─ READY_FOR_CREDENTIALS
          ↓
submit_credentials_once()        # bank adapter; one ordinary submission only
          ↓
checkpoint(POST_SUBMIT) loop
  ├─ AUTHENTICATED
  ├─ DISMISSIBLE_NOTICE ─────────→ continue loop
  ├─ DUPLICATE_SESSION ──────────→ confirm; continue loop
  ├─ PROTOCOL_RESUBMIT ──────────→ bounded second submit only for explicit bank rule
  ├─ CAPTCHA_RETRY ───────────────→ bounded second submit only after explicit bank captcha code
  ├─ STARTUP_RECOVERY ───────────→ bounded reload; return PRE_SUBMIT
  ├─ OTP_REQUIRED ───────────────→ stop with interaction_required
  ├─ PASSWORD_CHANGE_OPTIONAL ───→ dismiss; continue loop
  ├─ PASSWORD_CHANGE_REQUIRED ───→ stop with interaction_required
  ├─ EXPLICIT_LOGIN_ERROR ───────→ stop; no retry except existing explicit CAPTCHA rule
  └─ UNKNOWN_BLOCKER / TIMEOUT ──→ stop; no retry
          ↓
checkpoint(POST_SUBMIT_SETTLE)   # final notification sweep
          ↓
collect() → logout()
```

## Safety invariants

1. **Credential submission budget is centralized.** Ordinary login allows exactly one submit. A second submit is legal only through either an explicit `PROTOCOL_RESUBMIT` transition bound to a proven bank gate such as Taishin's 「上次未正常登出 → 重新登入」flow, or an explicit `CAPTCHA_RETRY` transition backed by a bank error code such as Sinopac's `captcha_invalid`. The two budgets never alias.
2. Startup reload, modal click, OTP detection, and notice dismissal never increment credential submit count.
3. CAPTCHA OCR retries before submit remain separate. Post-submit CAPTCHA retry is a typed `CAPTCHA_RETRY` checkpoint outcome, limited to one explicit bank response code; unknown/login-error text can never produce it.
4. OTP is a typed terminal interaction requirement. The common layer may identify the field/channel but never guesses, reads messages, or submits OTP automatically.
5. Password change is split into optional reminder versus mandatory requirement. Optional may click an exact negative action; mandatory stops. The crawler never changes the user's password.
6. Duplicate-session confirmation requires an exact scoped rule and a unique visible action. Generic keywords such as `確認／同意／OK` are not globally clickable.
7. Informational notifications use exact dismiss-only actions. Global safe labels may include `關閉`, `我知道了`, `稍後再看`, `略過`, `略過了`, `下次再說`, `Later`, `Skip`; all business-bearing actions require a bank-scoped rule.
8. Use Playwright Locator actionability and normal `click()`. No `HTMLElement.click()`, force click, `display:none`, backdrop removal, `modal-open` mutation, or Escape spam.
9. Containers with visible credential／OTP／CAPTCHA／password-change form controls are not generic notices.
10. Each transition has a bounded action count and requires an observable state change before looping.
11. Logs contain only bank, phase, checkpoint kind, rule name, action label, and budget counters. Never log modal bodies, OTP values, names, account numbers, or personalized campaign content.
12. Unknown states fail closed before another credential submission.

## Non-goals

- 不自動取得或填寫 OTP。
- 不自動變更網銀密碼。
- 不讓 global heuristic 決定「確認／同意」是否安全。
- 不把 native JS `alert/confirm/prompt` 全面重寫；先讓 checkpoint engine 接 DOM gates，native dialog 另做 security audit。
- 不在規劃階段執行真銀行登入、sync、push 或 deploy。

---

### Task 1: Build the typed checkpoint model and reducer

**Objective:** 先用純 Python 鎖死狀態、action 與 budget，不碰 browser。

**Files:**
- Create: `backend/core/login_checkpoints.py`
- Create: `tests/test_login_checkpoint_state_machine.py`

**Step 1: Write failing state-machine tests**

```python
def test_unknown_state_never_requests_resubmit(): ...
def test_otp_is_terminal_interaction_required_without_action(): ...
def test_optional_password_change_can_dismiss_but_mandatory_cannot(): ...
def test_startup_reload_does_not_consume_submission_budget(): ...
def test_duplicate_confirm_does_not_consume_submission_budget(): ...
def test_protocol_resubmit_requires_explicit_rule_and_one_remaining_budget(): ...
def test_third_submission_is_impossible(): ...
def test_explicit_captcha_retry_is_not_inferred_from_unknown_error(): ...
```

Run:

```bash
uv run pytest tests/test_login_checkpoint_state_machine.py -q
```

Expected: FAIL because the module does not exist.

**Step 2: Implement the minimum types**

```python
class CheckpointPhase(StrEnum):
    PRE_SUBMIT = "pre_submit"
    POST_SUBMIT = "post_submit"
    POST_SUBMIT_SETTLE = "post_submit_settle"


class CheckpointKind(StrEnum):
    AUTHENTICATED = "authenticated"
    READY_FOR_CREDENTIALS = "ready_for_credentials"
    DISMISSIBLE_NOTICE = "dismissible_notice"
    DUPLICATE_SESSION = "duplicate_session"
    PROTOCOL_RESUBMIT = "protocol_resubmit"
    CAPTCHA_RETRY = "captcha_retry"
    STARTUP_RECOVERY = "startup_recovery"
    OTP_REQUIRED = "otp_required"
    PASSWORD_CHANGE_OPTIONAL = "password_change_optional"
    PASSWORD_CHANGE_REQUIRED = "password_change_required"
    EXPLICIT_LOGIN_ERROR = "explicit_login_error"
    UNKNOWN_BLOCKER = "unknown_blocker"


@dataclass(frozen=True)
class LoginBudget:
    credential_submissions: int = 0
    protocol_resubmits: int = 0
    captcha_resubmits: int = 0
    reloads: int = 0


@dataclass(frozen=True)
class CheckpointOutcome:
    kind: CheckpointKind
    rule_name: str | None = None
    action_label: str | None = None
    interaction: str | None = None
```

`LoginBudget.__post_init__` rejects impossible states: submissions 0/1 require both resubmit counters 0; submissions 2 require exactly one resubmit counter 1; submissions >2, mixed resubmit counters, negative counters, or reloads >1 are invalid.

Add a pure reducer that accepts explicit current phase + budget + outcome and returns the next phase/budget or raises a typed terminal exception. It must never infer phase from submission count, and retry outcomes require a non-empty `rule_name`. No browser objects enter this reducer.

**Step 3: Verify reducer RED→GREEN**

```bash
uv run pytest tests/test_login_checkpoint_state_machine.py -q
uv run ruff check backend/core/login_checkpoints.py tests/test_login_checkpoint_state_machine.py
```

---

### Task 2: Add declarative gate rules and the browser executor

**Objective:** Centralize gate detection and safe action execution behind one interface.

**Files:**
- Modify: `backend/core/login_checkpoints.py`
- Create: `tests/test_login_checkpoint_browser_rules.py`

**Step 1: Add failing browser-rule tests**

Cover:

- exact container + body marker + unique action;
- hidden/outside/nested duplicate controls ignored;
- form-bearing modal is not treated as informational notice;
- ambiguous two-button match produces `UNKNOWN_BLOCKER` and zero clicks;
- body text is never included in result/log output;
- child iframe rule works;
- action must hide/detach or change selected state before another loop;
- bank-scoped `確認` works only inside its exact rule; global `確認` remains denied.

**Step 2: Implement declarative rules**

```python
@dataclass(frozen=True)
class LoginCheckpointRule:
    name: str
    bank: str
    phases: tuple[CheckpointPhase, ...]
    kind: CheckpointKind
    container_selector: str
    action_selector: str = "button, a, [role=button]"
    action_texts: tuple[str, ...] = ()
    required_body_pattern: re.Pattern[str] | None = None
    max_actions: int = 1
```

Expose one deep interface:

```python
def evaluate_login_checkpoint(
    page,
    *,
    bank: str,
    phase: CheckpointPhase,
    rules: tuple[LoginCheckpointRule, ...],
    is_authenticated: Callable[[Any], bool],
) -> CheckpointOutcome:
    ...
```

The function may execute only the rule-approved action and returns one outcome per call. The outer reducer owns loops/budgets.

Rule ownership and action safety are construction-time invariants: `rule.bank` must exactly match the evaluator's bank and is validated before any authentication short-circuit; `AUTHENTICATED`/`READY_FOR_CREDENTIALS` cannot be declared as DOM rules; CAPTCHA retry, startup recovery, OTP, mandatory password change, explicit error, and unknown blocker are classifier-only and never click. Every container selector and custom action selector must already be canonical (no leading/trailing whitespace) and use a conservative single-compound scoped grammar: optional tag plus at least one id, class, or exact-value attribute; wildcard, root/bare tags, unions, combinators, pseudos, presence-only attributes, fuzzy attribute operators, and attribute flags are rejected. Clickable notice/duplicate/protocol-resubmit/optional-password rules require either explicit nonblank exact action texts with the fixed broad action-element selector or a scoped structural selector; bare `[role=button]` is not a textless structural action. Structural actions return no DOM-derived `action_label`, and both Playwright and Patchright timeout types enter the same bounded alternate-progress check. PRE/POST keep authentication-first evaluation after ownership validation; POST_SUBMIT_SETTLE is rules-first, then authenticates only when no settle rule matched, so a visible post-login notice cannot be skipped.

**Step 3: Verify**

```bash
uv run pytest tests/test_login_checkpoint_browser_rules.py -q
uv run ruff check backend/core/login_checkpoints.py tests/test_login_checkpoint_browser_rules.py
```

---

### Task 3: Introduce the staged BankCrawler login interface in parallel

**Objective:** Create the seam required for post-submit gates without breaking all 13 banks at once.

**Files:**
- Modify: `backend/core/base.py:472-720`
- Modify: `tests/test_bank_collect_result_contract.py:244-355`
- Create: `tests/test_bank_login_lifecycle.py`

**Step 1: Add failing lifecycle tests**

Required event order:

```python
[
    "attach-dialog",
    "prepare-login-page",
    "checkpoint:pre-submit",
    "submit-credentials:1",
    "checkpoint:post-submit",
    "checkpoint:authenticated",
    "checkpoint:settle",
    "collect",
    "logout",
]
```

Negative controls:

- OTP ends before collect and before a second submit.
- Unknown blocker ends before a second submit.
- Startup recovery returns to pre-submit with `reloads=1`, submissions still 0.
- Explicit duplicate-session confirm loops without incrementing submissions.
- Explicit Taishin protocol-resubmit allows exactly submissions 1→2; any third attempt raises.
- Explicit Sinopac captcha retry allows exactly submissions 1→2 through a separate captcha budget; unknown/error-text paths cannot request it.

**Step 2: Add the new methods without cutting old `login()` yet**

```python
def prepare_login_page(self, page) -> None: ...
def is_authenticated(self, page) -> bool: ...
def submit_credentials_once(self, page) -> None: ...
def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]: ...
```

Keep the old abstract `login()` as a compatibility adapter for unmigrated banks during this task. Add an opt-in class flag such as `USES_SHARED_LOGIN_CHECKPOINTS = False`; only migrated banks enter the new base flow. This is a parallel migration, not a big-bang cutover.

**Step 3: Implement common orchestration for opted-in banks**

- `prepare_login_page()` may hydrate/open the login form but cannot read credentials.
- PRE_SUBMIT checkpoint runs before any credential call.
- `submit_credentials_once()` is invoked only through the centralized budget guard.
- POST_SUBMIT outcome loop is bounded.
- `POST_SUBMIT_SETTLE` runs before collect.
- Active rules are phase-exact and filtered before evaluator side effects: the base validates ownership across the full declared tuple before any auth shortcut and removes phase-ineligible rules. A clickable rule that exhausts `max_actions` or protocol authorization is replaced by a same-scope classifier-only UNKNOWN shadow rather than dropped; a still-visible blocker therefore fails closed without another click, while a hidden blocker permits normal fallback.
- Every rule-owned outcome must name an active rule of the same kind before reduction; only UNKNOWN may safely carry the name of a differently-kind active rule to report ambiguity/no-progress.
- Typed interaction requirements become `result["error"]` through the existing exception path.

**Step 4: Verify compatibility**

```bash
uv run pytest tests/test_bank_login_lifecycle.py tests/test_bank_collect_result_contract.py -q
```

Existing `_GoodCrawler` legacy path must still pass unchanged.

---

### Task 4: Migrate the proven gate-heavy banks

**Objective:** Prove the architecture on every known gate kind before broad cutover.

**Files:**
- Modify: `backend/banks/cathay.py`
- Modify: `backend/banks/ctbc.py`
- Modify: `backend/banks/rakuten.py`
- Modify: `backend/banks/ubot.py`
- Modify: `backend/banks/linebank.py`
- Modify: `backend/banks/taishin.py`
- Modify: `backend/banks/esun.py`
- Modify: `backend/banks/scb.py`
- Modify: `tests/test_ctbc_collector_validate.py`
- Modify: `tests/test_rakuten_crawler.py`
- Add focused cases to `tests/test_login_checkpoint_browser_rules.py`

**Step 1: Cathay and CTBC pre-submit gates**

- Cathay: `#divSystemLoginMsgList.show`, scoped sequence `下一 → ... → 我知道了/關閉/確定`, max 12.
- Cathay submit must become a one-click fail-closed helper. Delete the `page.click()` exception fallback to `NormalDataCheck()` because a click timeout does not prove the request was not dispatched; add a regression asserting exactly one submit-side action.
- CTBC: body `重要公告` + structural `a.btn_close`; security modal and page-global close remain untouched.
- CTBC: classify its currently logged-only OTP signal as terminal `OTP_REQUIRED` instead of waiting to generic failure.
- Remove `_dismiss_announcements()` and `_close_entry_announcement()` only after policy tests pass.

**Step 2: Rakuten all gate types**

Declarative rules:

- startup recovery `#ib_init_connect_error_popup` → `RELOAD_ONCE`, max reload 1;
- duplicate-session exact body → `DUPLICATE_SESSION`, unique `是，我要登入`;
- OTP field → `OTP_REQUIRED`, no click;
- referral/RICB and future exact dismiss-only action → `DISMISSIBLE_NOTICE`;
- unknown modal → `UNKNOWN_BLOCKER`.

Retain late first-navigation checkpoint invocation, but route it through the same shared evaluator. Delete `_recover_startup_connection`, `_resolve_duplicate_login_modal`, `_dismiss_known_promo`, and `_session_ready` only after equivalent tests exist.

**Step 3: UBot password gates**

- optional password expiry + exact negative action → `PASSWORD_CHANGE_OPTIONAL`;
- mandatory password form with no safe negative action → `PASSWORD_CHANGE_REQUIRED`;
- remove broad `_close_popups()` regex after both branches are tested.

**Step 4: LINE Bank and informational confirmation**

- Scope `確定／確認` to the exact login-success modal rule.
- Remove page-global `_dismiss_post_login_modal()`.

**Step 5: E.SUN, SCB, Taishin duplicate/OTP flows**

- E.SUN: replace generic `handle_dup_login_modal()` and "OTP means login ongoing" return with typed rules; OTP must stop as interaction-required, not flow into collect.
- SCB: migrate custom duplicate-session polling/preview/action to exact declarative rule; body preview is no longer logged.
- Taishin: duplicate gate may return `PROTOCOL_RESUBMIT`; central budget permits one protocol resubmit only after exact popup evidence. Remove local two-stage retry orchestration and force-hide popup code.
- Sinopac migration in Task 5 must return `CAPTCHA_RETRY` only from its existing structured `captcha_invalid` classification; preserve its current cardinality tests unchanged.

**Step 6: Focused verification**

```bash
uv run pytest \
  tests/test_login_checkpoint_state_machine.py \
  tests/test_login_checkpoint_browser_rules.py \
  tests/test_bank_login_lifecycle.py \
  tests/test_ctbc_collector_validate.py \
  tests/test_rakuten_crawler.py \
  tests/test_scb_captcha_retry.py -q
```

---

### Task 5: Migrate remaining banks and remove the compatibility path

**Objective:** Close the interface across all concrete BankCrawler subclasses.

**Files:**
- Modify: `backend/banks/dbs.py`
- Modify: `backend/banks/fubon.py`
- Modify: `backend/banks/hsbc.py`
- Modify: `backend/banks/scsb.py`
- Modify: `backend/banks/sinopac.py`
- Modify: `backend/core/base.py`
- Modify/add their focused login tests as discovered during migration.

**Step 1: Mechanical staged-interface migration**

For each bank:

- move hydration/open-login-modal work into `prepare_login_page()`;
- expose existing `_logged_in()` through `is_authenticated()`;
- move exactly one fill/captcha/submit path into `submit_credentials_once()`;
- translate existing post-submit error extraction into checkpoint rules/outcomes;
- translate explicit CAPTCHA-code retry into the shared `CAPTCHA_RETRY` outcome while preserving the bank's structured classifier and tests; never map unknown text to it.
- DBS and HSBC receive `OTP_REQUIRED` rules only from observed selectors/fixtures; TODO text alone is not enough to invent a matcher. Until observed evidence exists, unknown 2FA pages stop fail-closed.
- HSBC's two Continue clicks remain one `submit_credentials_once()` budgeted flow; add a cardinality test that central credential budget increments once while both protocol steps execute.
- SCSB pre/post modal cleanup becomes scoped notice rules and its login-page reload consumes the central reload budget; delete force-hide/backdrop removal only after replacement tests pass.
- Fubon header/login-modal opening must use normal Locator click, not synthetic `HTMLElement.click()`.

**Step 2: Static closure test**

Add an AST test requiring every concrete crawler to implement/opt into the staged interface and forbidding custom monolithic `login()` overrides after cutover.

**Step 3: Remove compatibility**

Delete `USES_SHARED_LOGIN_CHECKPOINTS` and the legacy abstract `login()` path only when all 13 banks pass the closure test.

---

### Task 6: Whole-tree safety audit, full tests, and exact commit

**Objective:** Prove no retry, privacy, or popup-safety regression remains.

**Step 1: Search every sibling path**

Use Hermes `search_files` over `backend/banks` and `tests` for:

```text
handle_dup_login_modal
_close_popups
_dismiss_.*modal
_dismiss_.*announcement
_recover_startup_connection
display:none
modal-backdrop
modal-open
keyboard.press("Escape")
```

Every raw hit must be removed, routed through the common checkpoint module, or explicitly documented as a non-login business modal. No silent deferral.

**Step 2: Submission-cardinality audit**

Run all login/retry suites and require existing ordinary max-attempt assertions to remain 1. The only planned 2-submit paths are Taishin's explicit protocol flow and Sinopac's bank-coded `captcha_invalid`; they use separate typed budgets.

```bash
uv run pytest \
  tests/test_sinopac_captcha_confidence.py \
  tests/test_scb_captcha_retry.py \
  tests/test_bank_login_lifecycle.py \
  tests/test_login_checkpoint_state_machine.py -q
```

**Step 3: Full gates**

```bash
uv run pytest -q
uv run ruff check backend tests
```

**Step 4: Exact diff review**

- no credential values, OTPs, modal bodies, screenshots, or raw payloads added;
- remove existing plaintext CAPTCHA logging from UBOT, Taishin, and SCB in touched login paths; log only length/confidence/status metadata;
- no dependency/lockfile changes;
- pre-existing untracked `marketing/` untouched;
- plan file included only if intentionally selected.

**Step 5: Amend the unpushed narrow commit**

`34cb160` is local/unpushed and its narrow architecture is superseded. Amend it:

```bash
git add backend/core/login_checkpoints.py backend/core/base.py backend/banks \
  tests/test_login_checkpoint_state_machine.py \
  tests/test_login_checkpoint_browser_rules.py \
  tests/test_bank_login_lifecycle.py tests/test_ctbc_collector_validate.py \
  tests/test_rakuten_crawler.py
git commit --amend -m "fix(banks): centralize login checkpoints"
```

No AI trailer; never add `marketing/`.

---

### Task 7: Authorized live gates and release

**Objective:** Separate source correctness from bank-runtime proof.

**Precondition:** 皇上逐家明示授權。每個 bank run 單獨執行、讀完結果再做下一家。

Priority live matrix:

1. Cathay: pre-submit multi-page announcement.
2. CTBC: `重要公告` structural close; security modal negative control.
3. Rakuten: startup recovery／duplicate session／OTP absence or typed stop／campaign dismissal.
4. UBot: optional password reminder versus mandatory change classification.
5. E.SUN／SCB／Taishin: duplicate-session transitions and submission counters.
6. LINE Bank: scoped login-success confirmation.

Formal command only:

```bash
.venv/bin/python -m cli.cli sync <bank> --headless
```

Rules:

- absence of a gate does not prove its rule;
- unknown post-submit state never causes automatic resubmit;
- OTP/password-required outcome is a correct safe stop, not a failed implementation;
- terminal exit 0 alone is insufficient—read raw page counts and local persistence;
- local live success does not authorize deploy.

After explicit deploy authorization: push main, wait for CI, build/push ACR, update ACA sitecontainer main image, verify healthy revision and Cloudflare-fronted `/healthz`, then run one explicitly authorized production sync and read authoritative job state/logs.

---

## Definition of Done

1. `BankCrawler.run()` owns pre-submit, post-submit, and settle checkpoint orchestration.
2. Every concrete bank uses `prepare_login_page()` + `is_authenticated()` + `submit_credentials_once()`; no monolithic bank `login()` remains.
3. Duplicate session, OTP, startup recovery, optional/mandatory password change, informational notices, authenticated, explicit error, and unknown blocker are typed common outcomes.
4. Ordinary credential submit count is 1; only explicit `PROTOCOL_RESUBMIT` or bank-coded `CAPTCHA_RETRY` can permit one bounded second submit, through separate budgets.
5. OTP and mandatory password change stop with typed interaction-required outcomes; no auto input/change.
6. Global actions exclude `確認／確定／OK／同意／下一／取消`; any use is scoped to an exact bank rule.
7. No login popup handler force-hides/removes DOM or logs modal bodies.
8. Focused reducer/browser/lifecycle tests include negative controls and true RED→GREEN evidence.
9. Full pytest and Ruff pass on the exact final tree.
10. No true-bank, push, deploy, or production claim is made without explicit authorization and authoritative output.
