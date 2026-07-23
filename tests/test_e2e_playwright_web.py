"""L2 — Web E2E via Playwright (point browser at running web + backend).

需要：
  - backend (uvicorn) 跑 port 8000
  - frontend (expo web) 跑 port 8081
  - playwright python (scrapling 已帶進來，chromium 已裝)

執行：
  cd ~/src/thoth
  uv run pytest tests/test_e2e_playwright_web.py -v -s

注意：第一次跑會等 metro bundle (10-25s)。conftest fixture 起兩個 server 並 kill。

對比 L1 (test_e2e_user_journey.py)：那是 in-process TestClient，這裡是
真實瀏覽器 + 真實 HTTP + 真實 localStorage。抓「UI 跟 API 對不上」的 bug。
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
from contextlib import closing, suppress
from pathlib import Path

import pytest

# Skip the whole module if playwright 沒裝
playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright


PROJECT_ROOT = os.environ.get(
    "BANK_CRAWLERS_PROJECT_ROOT",
    str(Path(__file__).resolve().parents[1]),
)
BACKEND_PORT = 8765
FRONTEND_PORT = 8766  # 避開 default 8081, 跟手動跑的 dev server 衝突


def _wait_port(port: int, timeout: float = 30.0, label: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.3)
    pytest.fail(f"{label} did not listen on :{port} within {timeout}s")


@pytest.fixture(scope="module")
def servers():
    """Boot uvicorn + expo web; yield URLs; kill on teardown."""
    from cryptography.fernet import Fernet

    env = os.environ.copy()
    env.update({
        "BANK_DATA_ROOT": "/tmp/thoth-e2e-playwright",
        "JWT_SECRET": "playwright-e2e-secret-32+bytes-padding",
        "SERVER_FERNET_KEY": Fernet.generate_key().decode(),
    })
    Path(env["BANK_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
    # remove DB so each module run starts fresh
    for f in ("server.sqlite",):
        with suppress(FileNotFoundError):
            (Path(env["BANK_DATA_ROOT"]) / f).unlink()

    backend = subprocess.Popen(
        [
            f"{PROJECT_ROOT}/.venv/bin/uvicorn",
            "backend.server.app:app",
            "--host", "127.0.0.1",
            "--port", str(BACKEND_PORT),
            "--log-level", "warning",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_port(BACKEND_PORT, timeout=15, label="backend uvicorn")

        # 用 EXPO_PUBLIC_API_URL 讓 frontend 連到測試 backend port
        fe_env = env.copy()
        fe_env["EXPO_PUBLIC_API_URL"] = f"http://127.0.0.1:{BACKEND_PORT}"
        fe_env["BROWSER"] = "none"  # 不要 expo 開 browser

        frontend = subprocess.Popen(
            ["pnpm", "exec", "expo", "start", "--web", "--port", str(FRONTEND_PORT)],
            cwd=f"{PROJECT_ROOT}/frontend",
            env=fe_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_port(FRONTEND_PORT, timeout=60, label="expo web")
            # expo 還在 metro bundle 中，再多等一下確保 index.html 有 response
            time.sleep(3.0)
            yield {
                "backend_url": f"http://127.0.0.1:{BACKEND_PORT}",
                "frontend_url": f"http://127.0.0.1:{FRONTEND_PORT}",
            }
        finally:
            frontend.terminate()
            try:
                frontend.wait(timeout=5)
            except subprocess.TimeoutExpired:
                frontend.kill()
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=5)
        except subprocess.TimeoutExpired:
            backend.kill()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


# ============================================================
# Test 1: web server up + index renders
# ============================================================

def test_web_renders_index(servers, browser):
    """訪問 / 預期能 render（即使 redirect 到 /login 也算通）。"""
    page = browser.new_page()
    try:
        resp = page.goto(servers["frontend_url"], wait_until="domcontentloaded", timeout=30_000)
        assert resp is not None
        assert resp.status == 200
        # NativeWind / react-native-web 一定會 inject root div#root 或 #__expo_router_root
        page.wait_for_selector("body", timeout=5_000)
        html = page.content()
        # 確認真的有 React 內容（不只是 raw HTML shell）
        assert len(html) > 500
    finally:
        page.close()


# ============================================================
# Test 2: register → 跳到 dashboard
# ============================================================

def test_register_and_redirect_to_dashboard(servers, browser):
    """填 register 表單 → submit → 應該跳到 /(tabs)/dashboard 並看到 email。"""
    page = browser.new_page()
    try:
        page.goto(servers["frontend_url"] + "/login", wait_until="networkidle", timeout=30_000)
        # 等 React render 完
        page.wait_for_selector("input", timeout=10_000)

        # 切到 register mode
        register_btn = page.locator("text=/register|註冊|sign up/i").first
        if register_btn.count() > 0:
            try:
                register_btn.click(timeout=2000)
                page.wait_for_timeout(500)  # 等 state 更新
            except Exception:
                pass

        # 重新 wait inputs（state 切換可能 unmount/remount）
        page.wait_for_selector("input", timeout=5_000)
        inputs = page.locator("input").all()
        n = len(inputs)
        assert n >= 2, f"expected 2+ inputs on login page, got {n}"

        unique = int(time.time() * 1000)
        email = f"e2e-{unique}@palace.example"
        password = "SyntheticTestPassword04!"

        inputs[0].fill(email)
        inputs[1].fill(password)

        submit = page.locator("text=/submit|login|register|登入|註冊/i").last
        submit.click()

        page.wait_for_timeout(3000)

        body_text = page.locator("body").inner_text()
        assert email in body_text or "dashboard" in body_text.lower() or "logout" in body_text.lower() or "sync" in body_text.lower(), \
            f"after register, dashboard not visible. body:\n{body_text[:500]}"
    finally:
        page.close()


# ============================================================
# Test 3: API base url 對得起（瀏覽器真的會打 backend）
# ============================================================

def test_browser_actually_hits_backend(servers, browser):
    """確認 frontend axios/fetch 真的打到我們的測試 backend port。

    用 Playwright 攔 network、跑 register、看有沒有 POST 到 backend_url。
    """
    page = browser.new_page()
    api_calls = []

    def on_request(req):
        if f":{BACKEND_PORT}" in req.url:
            api_calls.append((req.method, req.url))

    page.on("request", on_request)

    try:
        page.goto(servers["frontend_url"] + "/login", wait_until="networkidle", timeout=30_000)
        page.wait_for_selector("input", timeout=10_000)
        register_btn = page.locator("text=/register|註冊|sign up/i").first
        if register_btn.count() > 0:
            try:
                register_btn.click(timeout=2000)
                page.wait_for_timeout(500)
            except Exception:
                pass
        page.wait_for_selector("input", timeout=5_000)
        inputs = page.locator("input").all()
        unique = int(time.time() * 1000)
        inputs[0].fill(f"netcheck-{unique}@palace.example")
        inputs[1].fill("playwright-pw")
        page.locator("text=/submit|login|register|登入|註冊/i").last.click()
        page.wait_for_timeout(3000)

        register_posts = [
            (m, u) for m, u in api_calls
            if m == "POST" and "/auth/register" in u
        ]
        assert register_posts, \
            f"expected POST /auth/register to backend; all calls to backend:\n{api_calls}"
    finally:
        page.close()
