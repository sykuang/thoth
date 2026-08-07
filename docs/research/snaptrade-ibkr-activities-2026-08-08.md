# SnapTrade × Interactive Brokers Activities／Transactions 現行契約研究

- 查核日期：2026-08-08
- 範圍：只使用 SnapTrade 官方開發文件、官方 OpenAPI、官方 Brokerage Support Matrix 與官方 SDK 原始碼。
- 限制：未讀取任何憑證，未呼叫真實 SnapTrade／券商帳戶 API，未修改實作檔。

## 結論摘要

1. **IBKR 的歷史活動有官方支援。** 官方 Brokerage Support Matrix 的 `Interactive Brokers`（slug：`INTERACTIVE-BROKERS-FLEX`）列出：
   - Transaction History Limit (Activities)：**Last 2 years**
   - Granularity of Transaction(Activity) Timestamp：**Second**
   - Types of Transaction(Activity)：**Buy/Sell、Dividend、Interest、Deposit/Withdraw、Adjustment、Option Events**
   - Data freshness：**Daily**
   - Status：**Generally Available**
2. 現行建議讀取端點是 **`GET https://api.snaptrade.com/accounts/{accountId}/activities`**；這個 account-level endpoint 明載「未 deprecated，且沒有 planned sunset」。不要再把 user-level `GET /activities` 當主要路徑。
3. **`GET /activities`（跨帳戶／unified user activities）已 deprecated**，且 2026-04-25 後註冊的客戶一律得到 HTTP 410。規格未列 IBKR 例外；對舊客戶它理論上仍可包含 IBKR，但現在不應依賴。
4. account-level activities 的 `startDate`、`endDate`、`offset`、`limit` 都是 optional；日期為 inclusive `YYYY-MM-DD`，`offset` 預設 0，`limit` 預設且最多 1000。回應含 `data` 與 `pagination`。
5. 不應用硬編碼 brokerage allowlist。OpenAPI 的 `Brokerage.has_reporting` **已 deprecated**；現行 API 沒有另一個非 deprecated 的 brokerage-level activities capability boolean。執行期應對每個 account 呼叫 account-level endpoint，並以 `account.sync_status.transactions` 與 connection `disabled` 判斷同步／新鮮度。官方 Support Matrix 可作產品 coverage 參考，但不是文件化的 capability API。
6. 目前 pin 的 `snaptrade-python-sdk==11.0.182` **已具備** `account_information.get_account_activities(...)` 與上述日期／offset／limit 參數；但該版預設 host 仍是 legacy `https://api.snaptrade.com/api/v1`。現行文件要求 canonical root path；legacy prefix 只被標示 deprecated，並非 account activities endpoint 本身 sunset。這是升級 SDK 的理由，但不能據此推論 IBKR 空資料是 endpoint 不支援。

## 1. IBKR 官方 coverage

官方 [SnapTrade Institution Support](https://support.snaptrade.com/brokerages) 的 `Transaction history` view 對 Interactive Brokers 公開以下資料：

| 欄位 | Interactive Brokers |
|---|---|
| Name | Interactive Brokers |
| Slug | `INTERACTIVE-BROKERS-FLEX` |
| Region | International |
| Status | Generally Available |
| Auth Type | API Key |
| Data freshness | Daily |
| Transaction History Limit (Activities) | Last 2 years |
| Granularity of Transaction(Activity) Timestamp | Second |
| Types of Transaction(Activity) | Buy/Sell, Dividend, Interest, Deposit/Withdraw, Adjustment, Option Events |
| Expected Connection Duration | Months |

Support Matrix 自己對欄位的定義是：

- `Transaction History Limit (Activities)`：SnapTrade 對該 institution 最遠能抓到的 transaction/activity history。
- `Granularity of Transaction(Activity) Timestamp`：回傳 transaction/activity 的時間精度。
- `Types of Transaction(Activity)`：SnapTrade 對該 institution 能回傳的活動類型。

因此，「IBKR 沒有交易紀錄是因為 SnapTrade 不支援 IBKR activities」與現行官方資料不符。比較合理的待查方向是：初次交易同步尚未完成、連線 disabled、要求日期超出最近兩年、分頁只抓了第一頁、該帳戶在範圍內確實沒有符合條件的活動，或上游同步異常。

來源：

- [Brokerage Integrations guide](https://docs.snaptrade.com/docs/integrations)（引導至官方 Support Matrix）
- [SnapTrade Institution Support / Brokerages](https://support.snaptrade.com/brokerages)（Interactive Brokers row，Transaction history view）

## 2. 現行推薦 activities endpoint

### 2.1 推薦：account-level endpoint

```http
GET https://api.snaptrade.com/accounts/{accountId}/activities
```

現行官方契約：

- endpoint 本身 **not deprecated**，且 **no planned sunset**；
- canonical path 是 `/accounts/{accountId}/activities`；
- legacy `/api/v1` prefix 會帶 `Deprecation: @1781222400`（文件標註 2026-06-12），該 deprecated 標記只針對 path prefix；
- 回傳指定帳戶全部已知歷史交易；
- 依 `trade_date` 反向時間排序；
- Daily cached data，每日更新一次，時間依 brokerage 而異；
- connection disabled 時仍會回傳最後 cached state，但不再有最新資料。

官方來源：

- [List account activities API reference](https://docs.snaptrade.com/reference/Account%20Information/AccountInformation_getAccountActivities)
- [官方 OpenAPI（commit `107e62b`），account activities](https://github.com/passiv/snaptrade-sdks/blob/107e62b/api.yaml#L1183-L1277)
- [Account Data guide](https://docs.snaptrade.com/docs/account-data)

### 2.2 不推薦：unified user-level endpoint

```http
GET https://api.snaptrade.com/activities
```

現行契約：

- **Deprecated**；官方要求「if possible」改用 account-level endpoint；
- 2026-04-25 之後註冊的所有客戶會收到 **HTTP 410 Gone**；
- 舊客戶可依 `accounts` 或 `brokerageAuthorizations` 篩選；後者優先；
- 單次最多 10,000 筆；沒有 `offset`／`limit`；官方建議用 `startDate`／`endDate` 分日期區間；
- 回傳順序沒有保證，需自行依 `trade_date` 排序。

它沒有列出 IBKR 排除條款，且 IBKR 在 Support Matrix 明列有 activities coverage；所以對仍可使用此 deprecated endpoint 的舊客戶，IBKR 並非文件上的不支援 brokerage。但新客戶會直接 410，不能把它視為可持續的 unified strategy。

官方來源：

- [Get transaction history for a user API reference](https://docs.snaptrade.com/reference/Transactions%20And%20Reporting/TransactionsAndReporting_getActivities)
- [官方 OpenAPI（commit `107e62b`），deprecated unified activities](https://github.com/passiv/snaptrade-sdks/blob/107e62b/api.yaml#L2990-L3074)

## 3. 日期與分頁的精確契約

### `GET /accounts/{accountId}/activities`

| 參數 | 必填 | 契約 |
|---|---:|---|
| `accountId` | 是 | SnapTrade account UUID，path param。 |
| `startDate` | 否 | inclusive，`YYYY-MM-DD`；省略時為 SnapTrade 已知的第一筆 `trade_date`。 |
| `endDate` | 否 | inclusive，`YYYY-MM-DD`；省略時為 SnapTrade 已知的最後一筆 `trade_date`。 |
| `offset` | 否 | integer，最小 0，預設 0。 |
| `limit` | 否 | integer，最小 1，預設 1000；endpoint 單次最多 1000。 |
| `type` | 否 | comma-separated transaction types。 |

回應 shape：

```json
{
  "data": [/* UniversalActivity */],
  "pagination": {
    "offset": 0,
    "limit": 1000,
    "total": 1234
  }
}
```

可靠迭代方式：固定同一組 `startDate`／`endDate`／`type`，從 `offset=0` 開始，每次將 offset 增加本頁實際筆數（或固定 limit），直到已取得筆數達 `pagination.total`。不能只把 `limit=1000` 當作已抓完。

**日期格式注意：** OpenAPI schema 是 `format: date`，description 明訂 `YYYY-MM-DD`。部分自動產生 example 顯示帶時間的 ISO 字串，與 schema／description 不一致；實作應以 `YYYY-MM-DD` 為準。

### Deprecated `GET /activities`

- `startDate`／`endDate` 仍是 optional、inclusive date；
- `accounts` 與 `brokerageAuthorizations` 是 optional comma-separated filters，後者優先；
- 沒有 offset pagination；最多 10,000 筆，官方建議自行切日期區間；
- 不保證排序。

## 4. 帳戶同步狀態語意

帳戶物件的 `sync_status.transactions` 是判讀空資料最重要的執行期訊號：

```json
{
  "sync_status": {
    "transactions": {
      "initial_sync_completed": true,
      "last_successful_sync": "2026-08-07",
      "first_transaction_date": "2024-08-08"
    },
    "holdings": {
      "initial_sync_completed": true,
      "last_successful_sync": "2026-08-08T...Z"
    }
  }
}
```

精確語意：

- `transactions.initial_sync_completed`
  - 只表示 initial transaction sync 是否完成。
  - 大量交易帳戶可能需較久；為 `false` 時，空 activities 不能解讀為「沒有交易」或「broker 不支援」。
- `transactions.last_successful_sync`
  - 表示「**截至哪一天的整日交易已完整同步**」。
  - 不是最後一筆交易日期，也不是最近一次嘗試同步的時間。
- `transactions.first_transaction_date`
  - SnapTrade 已知的第一筆交易日期。
  - 不保證券商帳戶在此之前沒有交易；IBKR 官方 coverage 上限是最近兩年。
- `holdings.*`
  - 是 holdings（positions／balances／recent orders 等）同步狀態，不可拿來證明 transactions 已同步。
- connection `disabled`
  - disabled 後不能取得 brokerage 最新資料，但 activities endpoint 仍可能回傳最後 cached state；所以「有資料」也不代表新鮮。

交易資料對 Real-time 與 Daily plans 都是 cached、每日更新一次，且文件說明會延後一天；**沒有 intraday transactions**。需要盤中成交活動時，應比較 recent orders，而不是期待 activities 即時出現。

相關端點／事件：

1. 讀 account sync status：
   - `GET /authorizations/{authorizationId}/accounts`（官方 syncing guide 建議逐 connection 取得）
   - `GET /accounts`／`GET /accounts/{accountId}` 的 Account schema 也含 `sync_status`
2. 要求前一日 transactions sync：
   - `POST /authorizations/{authorizationId}/transactions/sync`
   - 非同步排程；不能產生 intraday transactions；disabled connection 回 402。
3. `ACCOUNT_TRANSACTIONS_UPDATED` webhook：
   - 只有建立新 transaction 時才送；若 sync 沒找到新資料，不會送 webhook。

官方來源：

- [Syncing and Data Freshness](https://docs.snaptrade.com/docs/syncing)
- [List accounts for a connection](https://docs.snaptrade.com/reference/Connections/Connections_listBrokerageAuthorizationAccounts)
- [List accounts](https://docs.snaptrade.com/reference/Account%20Information/AccountInformation_listUserAccounts)
- [Sync transactions for a connection](https://docs.snaptrade.com/reference/Experimental%20Endpoints/Connections_syncBrokerageAuthorizationTransactions)
- [官方 OpenAPI：AccountSyncStatus／TransactionsStatus](https://github.com/passiv/snaptrade-sdks/blob/107e62b/api.yaml#L3557-L3584)

## 5. 應取代 hardcoded allowlist 的欄位／策略

### 5.1 不要用 `Brokerage.has_reporting`

官方 Brokerage schema 仍看得到：

```yaml
has_reporting:
  deprecated: true
  description: This field is deprecated. Please contact us if you have a valid use case for it.
```

因此它不是新的 allowlist 替代品，也不應新增依賴。官方來源：

- [List all connections API reference](https://docs.snaptrade.com/reference/Connections/Connections_listBrokerageAuthorizations)
- [官方 OpenAPI：Brokerage.has_reporting deprecated](https://github.com/passiv/snaptrade-sdks/blob/107e62b/api.yaml#L5814-L5884)

### 5.2 現行可用的非硬編碼訊號

建議分成兩層：

1. **執行期（應用邏輯）**
   - 不做 brokerage allowlist；對所有適合的 investment accounts 呼叫 `GET /accounts/{accountId}/activities`。
   - 用 `sync_status.transactions.initial_sync_completed`、`last_successful_sync`、`first_transaction_date` 解釋 pending／freshness／known coverage。
   - 再用 connection `disabled` 判斷是否只剩 stale cache。
   - 空 `data` 只能表示查詢範圍內沒有已知活動，不能單獨等同 brokerage unsupported。
2. **產品說明／預期 coverage**
   - 以官方 Support Matrix 的 institution slug + `Transaction History Limit (Activities)`、`Types of Transaction(Activity)`、timestamp granularity 為人工查核來源。
   - 這些欄位目前沒有在公開 OpenAPI 中以非 deprecated、machine-readable brokerage capability object 暴露；不應爬公開 Notion 頁面作 production gate。

換言之，現行契約沒有一個可安全寫成 `if brokerage.supports_activities` 的 replacement boolean。最小且正確的修正方向是**移除 allowlist，讓 account-level API 與 account sync status 成為 truth source**，而不是換另一份 slug 清單。

## 6. 目前 pin：`snaptrade-python-sdk==11.0.182`

官方 tag `v11.0.182-python`（commit `c910f22d44276540050062df03e8c3eacfae9758`）已包含：

```python
snaptrade.account_information.get_account_activities(
    account_id=...,
    user_id=...,
    user_secret=...,
    start_date=...,
    end_date=...,
    offset=...,
    limit=...,
    type=...,
)
```

所以不需要升級才能「取得 account-level activities 方法」；該方法與 pagination 在 11.0.182 已存在。

但該版 `Configuration` 預設 host 是：

```python
https://api.snaptrade.com/api/v1
```

現行 SDK 主線改成：

```python
https://api.snaptrade.com
```

這與現行 API reference 對 canonical root path 的要求一致。官方文件只說 legacy `/api/v1` prefix deprecated，並明確說 activities endpoint 本身沒有 planned sunset；因此：

- **SDK upgrade 應排入**，避免長期依賴 legacy path；
- **不要把 SDK pin 直接當作 IBKR activities 空白的已證實根因**；11.0.182 已有正確 account endpoint，且官方未說 legacy prefix 現在會讓 IBKR 回空陣列。

官方來源：

- [官方 SDK tag `v11.0.182-python`：account activities OpenAPI](https://github.com/passiv/snaptrade-sdks/blob/v11.0.182-python/api.yaml#L920-L1010)
- [官方 SDK tag `v11.0.182-python`：Python method](https://github.com/passiv/snaptrade-sdks/blob/v11.0.182-python/sdks/python/snaptrade_client/paths/accounts_account_id_activities/get.py#L538-L571)
- [官方 SDK release `v11.0.182-python`](https://github.com/passiv/snaptrade-sdks/releases/tag/v11.0.182-python) 的 `configuration.py:121`：legacy default host
- [官方 SDK current master（commit `107e62b`）](https://github.com/passiv/snaptrade-sdks/commit/107e62b) 的 `sdks/python/snaptrade_client/configuration.py:128-132`：canonical default host

## 7. 對 IBKR「沒有交易紀錄」的契約層診斷順序

不需 brokerage allowlist，依序檢查：

1. 連線的 `brokerage.slug` 是否為官方 IBKR integration（Support Matrix 顯示 `INTERACTIVE-BROKERS-FLEX`）。
2. connection 是否 `disabled=true`；若是，現有結果只是最後 cache。
3. account 的 `sync_status.transactions.initial_sync_completed` 是否為 `true`。
4. `last_successful_sync` 是否已涵蓋欲查詢的完整日期；別把它誤認成最後交易日期。
5. 查詢區間是否落在 IBKR 的 **Last 2 years** coverage 內，並使用 `YYYY-MM-DD` inclusive dates。
6. 是否完整走完 `pagination.total`，而非只拿第一頁 1000 筆。
7. 若需要的是今日盤中成交，改查 recent orders；activities 不提供 intraday transactions。
8. 以上都正常但仍空，再以 SnapTrade request ID、account ID、connection ID 與 sync status 聯絡 SnapTrade support；不能從空陣列本身推斷不支援。

## 最終判定

- **`GET /accounts/{accountId}/activities`：官方現行推薦，IBKR 有支援。**
- **`GET /activities` unified：IBKR 沒有被排除，但 endpoint 已 deprecated，且新客戶 410，不應使用。**
- **沒有新的 non-deprecated brokerage capability boolean 可直接取代 allowlist。** `has_reporting` 已 deprecated；應移除 allowlist，改以 account-level endpoint + `sync_status.transactions` + connection `disabled` 判定執行期狀態，Support Matrix 只作 coverage 參考。
