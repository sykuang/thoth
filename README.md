# Thoth：麻布記帳 Moneybook 的開源替代方案｜台灣銀行自動記帳

<p align="center">
  <img src="assets/logo.png" alt="Thoth 麻布記帳 Moneybook 開源替代方案標誌" width="160" height="160">
</p>

<p align="center">
  <a href="https://github.com/sykuang/thoth/actions/workflows/ci.yml"><img src="https://github.com/sykuang/thoth/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12%2B-blue.svg" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT License"></a>
</p>

**Thoth 是麻布記帳（Moneybook）的非官方開源、自架替代方案**，專注於自動同步台灣銀行帳戶、交易明細與信用卡帳單，也能管理手動帳戶並透過 SnapTrade 整合券商資產。它將 13 家銀行與其他資產來源整理成一致格式，透過 FastAPI API 與 Expo 應用程式提供個人財務管理、收支分析、投資持倉、交易分類與繳款提醒。

如果你正在尋找「麻布記帳替代品」、「台灣銀行自動記帳」、「銀行帳戶整合」或「self-hosted personal finance」工具，Thoth 提供可自行部署、可審查原始碼、可擴充銀行連接器的 MIT 授權方案。

名稱取自古埃及掌管書寫、計算與紀錄的神祇 Thoth，代表本專案最重要的原則：**如實保存原始資訊，不為了畫面完整而捏造財務資料。**

> [!IMPORTANT]
> Thoth 與[麻布記帳 Moneybook](https://moneybook.com.tw/)／麻布數據科技沒有隸屬、合作或授權關係，也不是其完整功能複製品。Moneybook 為其權利人所有的產品與商標；本專案僅以「開源替代方案」描述使用情境。

> [!WARNING]
> 本專案僅供個人使用、技術研究與開源學習。銀行網站可能禁止未經授權的自動化存取；使用者必須自行確認所在地法律、銀行服務條款與帳戶安全要求。請勿用於存取他人帳戶、規避銀行限制或商業轉售資料。

## 應用畫面

<p align="center">
  <img src="docs/screenshots/app-store/01-hero.webp" alt="Thoth 一眼看懂財務全貌" width="23%">
  <img src="docs/screenshots/app-store/02-two-devices.webp" alt="Thoth 銀行與信用卡一次掌握" width="23%">
  <img src="docs/screenshots/app-store/03-device-top.webp" alt="Thoth 每筆收支清楚呈現" width="23%">
  <img src="docs/screenshots/app-store/04-device-bottom.webp" alt="Thoth 所有帳戶一鍵同步" width="23%">
</p>

<p align="center">
  <img src="docs/screenshots/app-store/05-no-device.webp" alt="Thoth self-hosted 財務資料自主掌控" width="30%">
  <img src="docs/screenshots/app-store/06-two-devices.webp" alt="Thoth 從財務總覽追到每筆明細" width="30%">
  <img src="docs/screenshots/app-store/07-no-device.webp" alt="Thoth 完整個人財務工具" width="30%">
</p>

<p align="center"><sub>財務總覽・帳戶整合・收支追蹤・一鍵同步・資料自主</sub></p>

> 畫面中的 Email、帳號、卡號、交易與金額均為合成展示資料，不含任何真實金融資訊。

## 為什麼選擇 Thoth 作為 Moneybook 開源替代方案？

[Moneybook 官網](https://moneybook.com.tw/)主打同步 30+ 家銀行、投資、電子票證與電子發票等全資產管理；Thoth 的目標不同：把「台灣銀行與信用卡自動同步、手動帳戶、券商持倉」做成可自行掌控、可持續維護的開源基礎設施。

| 比較面向 | Thoth | 麻布記帳 Moneybook |
|---|---|---|
| 產品型態 | MIT 開源、可 self-host | 商業理財記帳服務 |
| 核心定位 | 台灣銀行、手動帳戶與券商資產聚合 | 銀行、投資、電子票證、發票等全資產管理 |
| 銀行範圍 | 13 家銀行連接器，支援深度依銀行而異 | 官網標示 30+ 家銀行與多元資產 |
| 其他資產 | 手動存款／負債／投資帳戶；可選 SnapTrade 券商同步 | 由官方服務提供多元資產整合 |
| 資料與部署 | 部署在自己的電腦或伺服器 | 使用官方 App 與服務 |
| 擴充方式 | 可修改 crawler、API、分類規則與 UI | 由官方產品團隊維護 |
| 適合對象 | 開發者、self-hoster、重視可審查性者 | 希望直接使用完整消費型 App 的一般使用者 |

Thoth 目前不包含 Moneybook 的所有功能，例如電子發票、電子票證與官方繳費服務；SnapTrade 覆蓋範圍也取決於其支援的券商與帳戶。它不是 drop-in replacement，而是一個聚焦台灣網銀、手動帳戶與券商資產的開源起點。

### 適合哪些人？

- 想找麻布記帳替代品，但希望資料與服務部署在自己掌控的環境
- 想自動同步多家台灣銀行帳戶、信用卡與交易明細
- 想把無法自動同步的存款、貸款、房貸或投資以手動帳戶納入淨資產
- 想透過 SnapTrade 將支援券商的帳戶、餘額、持倉與活動整合進同一介面
- 想研究台灣 Open Banking、網路銀行 SPA 與金融資料正規化
- 想在 FastAPI、Expo、SQLite／PostgreSQL 基礎上打造自己的記帳 App
- 願意因應銀行改版維護 crawler，並理解自架服務的安全責任

## 主要功能

- 整合 13 家台灣銀行的登入、帳戶、交易與信用卡資料
- 支援手動建立存款、定存、外幣、支票、貸款、房貸、信用額度與投資帳戶
- 可選用 SnapTrade 同步券商帳戶、現金餘額、持倉與活動紀錄
- 將存款交易、信用卡已入帳與未入帳消費正規化為一致資料模型
- 支援同一使用者管理多家銀行與多個銀行帳戶
- 以 Fernet 加密保存銀行登入資料，不在資料庫中儲存明文密碼
- 提供月收入、支出、淨額、分類與銀行別統計
- 支援交易分類、Hashtag、忽略狀態與使用者自訂描述
- 提供繳款提醒、同步排程、WebSocket 同步進度與可選推播通知
- 前端支援 Web 與 iOS；桌面版可透過 Tauri 封裝
- 伺服器資料庫可使用 SQLite，亦可切換 PostgreSQL

## 支援銀行

目前 `sync_runner.SUPPORTED_BANKS` 登記 13 家銀行。下表依目前程式碼實際寫入 canonical store 的路徑整理；它描述的是**已實作能力**，不保證銀行網站此刻可用。

| 代碼 | 銀行 | 存款帳戶／餘額 | 存款交易 | 信用卡資料 |
|---|---|---|---|---|
| `cathay` | 國泰世華 | ✅ | ✅ | ✅ 卡片、已入帳、未入帳 |
| `ctbc` | 中國信託 | ✅ | ✅ | ✅ 卡片、已入帳、未入帳 |
| `dbs` | 星展銀行 | ✅ | ◐ 交易 endpoint／parser | ◐ 卡片與應繳摘要 |
| `esun` | 玉山銀行 | ✅ | ✅ | ✅ 卡片、已入帳、未入帳 |
| `fubon` | 台北富邦 | ✅ | ✅ | ✅ 卡片、已入帳、未入帳 |
| `hsbc` | 滙豐銀行 | — 信用卡專用 | — | ✅ 卡片、已入帳、未入帳 |
| `linebank` | LINE Bank | ✅ | ✅ | — |
| `rakuten` | 樂天國際銀行 | ✅ | ✅ | — |
| `scb` | 渣打銀行 | ◐ 總覽摘要 | — | ◐ 卡片與已入帳；未入帳未實作 |
| `scsb` | 上海商銀 | ✅ | ◐ 依頁面表格可解析性 | ◐ 即時／未入帳與帳單摘要 |
| `sinopac` | 永豐銀行 | ✅ | ✅ | ✅ 卡片、已入帳、未入帳 |
| `taishin` | 台新銀行 | ✅ | ✅ | ✅ 卡片、已入帳、未入帳 |
| `ubot` | 聯邦銀行 | ✅ | ✅ | ✅ 卡片、已入帳、未入帳 |

`✅` 代表已有 canonical store 寫入路徑；`◐` 代表摘要、條件式 parser 或尚未覆蓋完整明細；`—` 代表目前沒有對應路徑。銀行頁面、登入驗證與 API 隨時可能改版；欄位完整度也會依帳戶產品與使用者實際持有資料而異。

### 手動帳戶與 SnapTrade

銀行連接器以外，Thoth 目前還有兩種資產來源：

| 來源 | 支援內容 | 注意事項 |
|---|---|---|
| 手動帳戶 | 存款、定存、外幣存款、支票帳戶、貸款、房貸、信用額度、投資 | 可手動維護餘額與是否納入淨資產；投資帳戶可保存交易與持倉，並在可用時以 Yahoo Finance 估值 |
| SnapTrade | 券商帳戶、現金餘額、持倉、交易／活動紀錄 | 可選整合；必須由伺服器設定 SnapTrade，實際券商與欄位依 SnapTrade 連線回傳為準 |

銀行同步、手動帳戶與 SnapTrade 資料會共同出現在帳戶與淨資產介面，但仍保留各自的資料來源與更新時間，不會把手動值冒充成銀行或券商即時資料。

## 系統架構

```text
┌──────────────────────────────┐
│ Expo / React Native 前端     │
│ Web・iOS・Tauri Desktop      │
└──────────────┬───────────────┘
               │ HTTPS / JWT / WebSocket
               ▼
┌──────────────────────────────┐
│ FastAPI Server               │
│ auth・accounts・sync・cards  │
│ transactions・portfolio     │
│ manual accounts・SnapTrade  │
└──────────┬───────────┬───────┘
           │           │
           ▼           ▼
┌─────────────────┐  ┌────────────────────┐
│ SQLite /        │  │ Bank / SnapTrade   │
│ PostgreSQL      │  │ Scrapling / REST   │
│ Manual + sync   │  │ CAPTCHA / SPA API  │
└─────────────────┘  └────────────────────┘
```

## 一體式 Docker：Frontend + Backend + SQLite

`Dockerfile.standalone` 會把 Expo Web 編譯成靜態檔，與 FastAPI、Playwright 銀行連接器及 SQLite 一起封裝成單一 image。Web 與 API 使用同一個 origin；SQLite 固定寫入 `/data`，必須掛載持久化 volume。

### GitHub Actions 一鍵建置

Fork repository 後，前往 **Actions → Publish standalone Docker image → Run workflow**。Workflow 會實際啟動 container，驗證 frontend、backend、SQLite 寫入與 restart persistence，再發布：

```text
ghcr.io/<你的 GitHub 帳號>/thoth:standalone
```

本 repository 的 workflow：<https://github.com/sykuang/thoth/actions/workflows/publish-standalone.yml>

### 啟動 standalone container

先建立 `.env` 並填入兩個必要 secret：

```bash
cp .env.example .env
openssl rand -hex 32
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

把輸出分別填入 `.env` 的 `JWT_SECRET` 與 `SERVER_FERNET_KEY`，即可用一個命令部署：

```bash
docker compose -f compose.standalone.yml up -d
```

預設使用 `ghcr.io/sykuang/thoth:standalone`；fork 可用 `THOTH_IMAGE=ghcr.io/<owner>/thoth:standalone` 覆寫。若不使用 Compose，也可直接啟動：

```bash
docker run -d \
  --name thoth \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file .env \
  -v thoth-data:/data \
  ghcr.io/sykuang/thoth:standalone
```

開啟 `http://localhost:8000`。環境變數使用 Docker 原生 `--env-file`／Compose `env_file` 注入，不需要掛載 `.env`。SQLite 預設使用 `thoth-data` named volume；若要掛載自己的目錄：

```bash
THOTH_DATA_SOURCE=/path/to/thoth-data docker compose -f compose.standalone.yml up -d
```

升級 image 時保留同一個 SQLite volume 與 `.env`；**不可遺失或任意更換 `SERVER_FERNET_KEY`**，否則既有銀行憑證將無法解密。

若不使用 GHCR，也可直接本機建置：

```bash
docker build -f Dockerfile.standalone -t thoth:standalone .
```

> Standalone SQLite image 適合單機 Docker host／NAS。請勿將 `/data` 放在 SMB／Azure Files；SQLite file locking 在網路檔案系統上不可靠。雲端多 replica 部署請改用 PostgreSQL 與原本的 backend-only `Dockerfile`。

## 快速開始

### 系統需求

- Python 3.12 以上
- [uv](https://docs.astral.sh/uv/)
- Node.js 20 以上
- pnpm 10.33.3
- macOS 或 Linux

### 1. 下載並安裝後端

```bash
git clone https://github.com/sykuang/thoth.git
cd thoth

uv sync --all-extras --dev
uv run scrapling install
```

### 2. 設定環境變數

```bash
cp .env.example .env
```

至少必須在 `.env` 填入：

| 變數 | 用途 | 產生方式 |
|---|---|---|
| `JWT_SECRET` | 簽署登入權杖 | `openssl rand -hex 32` |
| `SERVER_FERNET_KEY` | 加密銀行登入資料 | `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

`SERVER_FERNET_KEY` 一旦用來加密既有資料後就不應任意更換，否則原有密文將無法解密。完整選項與各銀行舊版 CLI 變數請參考 [`.env.example`](.env.example)。

### 3. 啟動後端

```bash
uv run uvicorn backend.server.app:app \
  --host 127.0.0.1 \
  --port 8000 \
  --reload
```

健康檢查：

```bash
curl http://127.0.0.1:8000/healthz
```

### 4. 啟動 Web 前端

```bash
cd frontend
corepack enable
corepack prepare pnpm@10.33.3 --activate
pnpm install --frozen-lockfile
pnpm web
```

預設可從 `http://localhost:8081` 開啟前端。第一次使用時請先註冊本機帳號，再到設定頁新增銀行帳戶與登入欄位。

## iOS 與桌面版

### iOS

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm ios
```

若原生專案需要重新產生：

```bash
pnpm exec expo prebuild --clean
cd ios
pod install
```

### Tauri 桌面版

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm desktop
```

建立正式安裝包：

```bash
pnpm desktop:build
```

## CLI 使用方式

不啟動 API 與前端時，也可以直接同步單一銀行或查看本機資料：

```bash
# 同步單一銀行
uv run python -m cli.cli sync sinopac --headless

# 查看資料庫摘要
uv run python -m cli.cli stats sinopac

# 顯示最近交易
uv run python -m cli.cli txns sinopac --limit 20
```

CLI 主要供連接器開發、除錯與個人排程使用。多使用者模式應優先從前端新增帳戶，讓登入資料以 Fernet 加密後寫入伺服器資料庫。

## 資料庫

### SQLite

SQLite 是預設模式，適合本機或單機自架。資料預設放在 `backend/data/`，該目錄已由 Git 忽略。

### PostgreSQL

若要使用 PostgreSQL：

```dotenv
DB_BACKEND=postgres
DATABASE_URL=postgresql://user:password@host:5432/thoth
```

公開部署時，請使用平台提供的 Secret 管理功能，不要把連線字串寫入映像檔、原始碼或 Git。

## 安全設計

- 銀行登入資料以 Fernet 加密後保存
- 使用者密碼以 bcrypt 雜湊保存
- API 使用 JWT Bearer 驗證
- 可選擇設定 `SERVER_API_KEY`，在 JWT 外再增加伺服器層 API Key
- 登入端點有依來源 IP 計算的失敗次數限制
- 開發環境預設只允許 localhost CORS；正式環境應明確設定 `CORS_ORIGINS`
- 預設測試不連線真實銀行，也不應包含任何真實帳號、卡號、身分證字號或登入憑證
- 銀行登入失敗時不應自動無限重試，以免觸發帳號鎖定

正式部署至少應做到：

1. 全程使用 HTTPS。
2. 將 Secret 放在部署平台的 Secret Store。
3. 僅開放必要來源與網路入口。
4. 定期備份資料庫，並驗證還原流程。
5. 不記錄密碼、Cookie、Token、完整帳號或完整卡號。

## 專案結構

```text
.
├── backend/
│   ├── banks/              # 各銀行連接器與資料擷取流程
│   ├── core/               # 共用 crawler、資料模型、分類與持久化
│   ├── server/             # FastAPI app、routers、auth、sync、push
│   └── tools/              # 維護與診斷工具
├── cli/                    # 單機 CLI
├── frontend/
│   ├── src/app/            # Expo Router 頁面
│   ├── src/lib/            # API client、格式化與遮罩工具
│   ├── src/stores/         # Zustand 狀態
│   ├── ios/                # Expo prebuild 產生的 iOS 專案
│   └── src-tauri/          # Tauri 桌面版
├── migrations/             # 資料庫遷移
├── tests/                  # pytest 測試
├── .env.example            # 環境變數範本
└── pyproject.toml          # Python 專案與工具設定
```

## 開發與測試

```bash
# 後端測試；設定會自動排除需要獨立環境的 Web E2E
uv run pytest -q

# Python lint
uv run ruff check backend/ cli/ tests/

# 前端型別檢查
cd frontend
pnpm typecheck

# 前端 lint
pnpm lint
```

CI 會對每次推送與 Pull Request 執行後端測試與前端型別檢查。詳細開發規範請見 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 隱私與測試資料

本 repository 只能包含合成測試資料。新增 fixture 時請遵守：

- 帳號、卡號、身分證字號、姓名、Email 與電話都必須是合成值
- 不要直接複製真實銀行頁面內容後提交
- 移除 Cookie、Token、Session ID、Request Header 與裝置識別資訊
- 遮罩後的卡號末四碼仍可能識別個人，不能直接沿用真實值
- `.env`、資料庫、瀏覽器 profile、HAR 與同步輸出不得加入 Git

## 常見問題（FAQ）

### Thoth 是什麼？

Thoth 是一套開源個人財務與自動記帳平台。它可同步多家台灣銀行帳戶、存款交易與信用卡資料，也支援手動帳戶與可選 SnapTrade 券商整合，並提供 Web、iOS 與可選桌面介面。

### Thoth 是麻布記帳 Moneybook 的官方版本嗎？

不是。Thoth 是獨立社群專案，與 Moneybook 或麻布數據科技沒有隸屬、合作或授權關係。

### Thoth 可以完全取代 Moneybook 嗎？

目前不能完整取代。Thoth 聚焦 13 家台灣銀行、手動帳戶與可選 SnapTrade 券商資料；Moneybook 官網列出的電子票證、電子發票與官方繳費等功能並非 Thoth 的完整支援範圍。

### Thoth 支援哪些台灣銀行？

目前包含國泰世華、中國信託、星展、玉山、台北富邦、滙豐、LINE Bank、樂天國際銀行、渣打、上海商銀、永豐、台新與聯邦共 13 家連接器。實際可取得的帳戶、交易與信用卡欄位依銀行而異，詳見上方支援矩陣。

### Thoth 可以加入無法自動同步的帳戶或券商嗎？

可以。手動帳戶可記錄存款、負債與投資，並選擇是否納入淨資產；伺服器完成 SnapTrade 設定後，也能同步其支援券商的帳戶、餘額、持倉與活動紀錄。

### Thoth 是否能 self-host？

可以。Backend 可在 macOS 或 Linux 上自行部署，預設使用 SQLite，也支援 PostgreSQL；Frontend 採 Expo／React Native，可執行於 Web 與 iOS，亦能透過 Tauri 封裝桌面版。

### 銀行帳密如何保存？

銀行登入資料會先以 Fernet 加密，再寫入伺服器資料庫。使用者仍須自行保護 `.env`、`SERVER_FERNET_KEY`、資料庫、備份與部署環境；請勿把真實憑證提交到 Git。

### Thoth 採用什麼開源授權？

Thoth 採用 MIT License，允許依授權條款使用、修改與散布。

## 法律與責任

本專案不是銀行官方產品，也不代表任何金融機構。銀行網站與 API 隨時可能變更，使用本專案可能導致登入失敗、帳號暫時鎖定或資料不完整。

使用者必須自行承擔安裝、執行、資料保管與法規遵循責任。維護者與貢獻者不對資料遺失、帳戶限制、服務中斷、法律爭議或其他直接與間接損害負責。

如果你的使用情境涉及金融機構、商業服務或大量使用者，請優先採用銀行正式合作管道或合規的 Open Banking API。

## 參與貢獻

歡迎修正銀行改版、補充 parser、增加測試、改善前端與文件。提交前請先閱讀 [`CONTRIBUTING.md`](CONTRIBUTING.md)，並確認：

- 測試與型別檢查通過
- 沒有真實個資、憑證或銀行 session 資料
- 新行為有對應測試
- UI 變更附上畫面或操作說明
- 銀行連接器不會在登入失敗後無限制重試

## 授權

本專案採用 [MIT License](LICENSE)。
