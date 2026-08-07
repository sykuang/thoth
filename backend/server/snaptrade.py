"""SnapTrade read-only brokerage integration.

The public interface is intentionally small: status, connection URL, atomic sync,
and a user-scoped local snapshot. SDK details and userSecret encryption stay here.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from cryptography.fernet import Fernet

from backend.server import db
from backend.server.db import now_iso

_PROVIDER = "snaptrade"
_ACTIVITY_PAGE_SIZE = 1000
_MAX_ACTIVITY_PAGES = 100
_MAX_TRANSACTION_SYNC_LAG = timedelta(days=7)


class SnapTradeNotConfigured(RuntimeError):
    pass


class SnapTradeNotRegistered(RuntimeError):
    pass


class SnapTradeBusy(RuntimeError):
    pass


class SnapTradeInvalidCallback(ValueError):
    pass


@contextmanager
def _operation_lock(user_id: int, operation: str, lease_seconds: int):
    owner = uuid4().hex
    acquired = datetime.now(UTC)
    expires = acquired + timedelta(seconds=lease_seconds)
    acquired_at = acquired.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    expires_at = expires.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if not db.snaptrade_acquire_lock(
        user_id, operation, owner, acquired_at, expires_at,
    ):
        raise SnapTradeBusy(f"SnapTrade {operation} 已在執行")
    try:
        yield owner
    finally:
        db.snaptrade_release_lock(user_id, operation, owner)


def _configured() -> bool:
    return bool(os.environ.get("SNAPTRADE_CLIENT_ID", "").strip()) and bool(
        os.environ.get("SNAPTRADE_CONSUMER_KEY", "").strip(),
    )


def _fernet() -> Fernet:
    key = os.environ.get("SERVER_FERNET_KEY", "").strip()
    if not key:
        raise SnapTradeNotConfigured("SERVER_FERNET_KEY 尚未設定")
    return Fernet(key.encode())


def _plain(value: Any) -> Any:
    """Convert generated SDK responses/models into plain dict/list values."""
    if hasattr(value, "body"):
        value = value.body
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _response(value: Any) -> Any:
    status = getattr(value, "status", 200)
    if not 200 <= status < 300:
        raise RuntimeError(f"SnapTrade HTTP {status}")
    return _plain(value)


def _raw_payload(api: Any, operation: str, **kwargs: Any) -> Any:
    """Use the pinned v11 transport without its lossy float deserializer."""
    mapped = getattr(api, f"_{operation}_mapped_args")(**kwargs)
    call_args = {
        "query_params": mapped.query or {},
        "skip_deserialization": True,
    }
    if mapped.path:
        call_args["path_params"] = mapped.path
    response = getattr(api, f"_{operation}_oapg")(**call_args)
    status = getattr(response, "status", 200)
    if not 200 <= status < 300:
        raise RuntimeError(f"SnapTrade HTTP {status}")
    body = getattr(response, "body", response)
    if isinstance(body, bytes):
        body = body.decode()
    if isinstance(body, str):
        body = json.loads(body, parse_float=Decimal)
    return body


def _raw_rows(api: Any, operation: str, **kwargs: Any) -> list[dict[str, Any]]:
    return _rows(_raw_payload(api, operation, **kwargs))


def _rows(value: Any) -> list[dict[str, Any]]:
    value = _plain(value)
    if isinstance(value, list):
        if not all(isinstance(row, dict) for row in value):
            raise RuntimeError("SnapTrade 2xx response rows 格式錯誤")
        return value
    if isinstance(value, dict):
        for key in ("results", "data", "items", "activities"):
            if key not in value:
                continue
            rows = value[key]
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise RuntimeError("SnapTrade 2xx response rows 格式錯誤")
            return rows
    raise RuntimeError("SnapTrade 2xx response envelope 格式錯誤")


def _nested(data: Mapping[str, Any], *path: str) -> Any:
    value: Any = data
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _decimal_text(value: Any) -> str | None:
    number = _decimal(value)
    return format(number, "f") if number is not None else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _slug(
    account: Mapping[str, Any],
    authorization_slugs: Mapping[str, str],
) -> str | None:
    authorization = account.get("brokerage_authorization")
    if text := _text(authorization):
        if slug := authorization_slugs.get(text):
            return slug
    candidates = (
        _nested(account, "brokerage_authorization", "brokerage", "slug"),
        _nested(account, "brokerage", "slug"),
        _nested(account, "institution", "slug"),
    )
    for value in candidates:
        if text := _text(value):
            return text.upper()
    institution = _text(account.get("institution_name"))
    return institution.upper().replace(" ", "-") if institution else None


def _dedupe_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop only exact duplicate provider IDs; names/numbers are not identities."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for account in accounts:
        account_id = _text(account.get("id"))
        if account_id and account_id in seen:
            continue
        if account_id:
            seen.add(account_id)
        result.append(account)
    return result


def _validate_redirect_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.path != "/investments" or parsed.query or parsed.fragment or parsed.params:
        raise SnapTradeInvalidCallback("不允許的 SnapTrade callback URL")
    if uri == "thoth:///investments":
        return uri
    if uri == "tauri://localhost/investments":
        return uri
    if uri in {
        "http://localhost:8081/investments",
        "http://127.0.0.1:8081/investments",
    }:
        return uri
    if parsed.scheme == "https":
        origin = f"https://{parsed.netloc}"
        allowed = {
            item.strip().rstrip("/")
            for item in os.environ.get("CORS_ORIGINS", "").split(",")
            if item.strip()
        }
        if origin in allowed:
            return uri
    raise SnapTradeInvalidCallback("不允許的 SnapTrade callback URL")


class SnapTradeSDKGateway:
    """Thin adapter around the generated SnapTrade Python SDK."""

    def __init__(self) -> None:
        if not _configured():
            raise SnapTradeNotConfigured("SnapTrade server credentials 尚未設定")
        from snaptrade_client import SnapTrade

        self.client = SnapTrade(
            host="https://api.snaptrade.com",
            consumer_key=os.environ["SNAPTRADE_CONSUMER_KEY"],
            client_id=os.environ["SNAPTRADE_CLIENT_ID"],
        )

    def register_user(self, user_id: str) -> dict[str, Any]:
        return _response(
            self.client.authentication.register_snap_trade_user(user_id=user_id),
        )

    def list_registered_user_ids(self) -> set[str]:
        response = _response(self.client.authentication.list_snap_trade_users())
        if isinstance(response, dict):
            response = response.get("users", response.get("data", []))
        if not isinstance(response, list):
            return set()
        return {
            user_id
            for item in response
            if (user_id := _text(
                item.get("userId") or item.get("user_id")
                if isinstance(item, Mapping) else item
            ))
        }

    def delete_user(self, user_id: str) -> None:
        _response(self.client.authentication.delete_snap_trade_user(user_id=user_id))

    def connection_url(
        self,
        user_id: str,
        user_secret: str,
        redirect_uri: str,
    ) -> str:
        response = _response(self.client.authentication.login_snap_trade_user(
            user_id=user_id,
            user_secret=user_secret,
            connection_type="read",
            show_close_button=True,
            custom_redirect=redirect_uri,
        ))
        url = (
            response.get("redirectURI") or response.get("redirect_uri")
            if isinstance(response, dict)
            else None
        )
        if not url:
            raise RuntimeError("SnapTrade 未回傳 Connection Portal URL")
        return str(url)

    def list_connections(self, user_id: str, user_secret: str) -> list[dict[str, Any]]:
        return _rows(_response(self.client.connections.list_brokerage_authorizations(
            user_id=user_id, user_secret=user_secret,
        )))

    def list_accounts(self, user_id: str, user_secret: str) -> list[dict[str, Any]]:
        return _raw_rows(
            self.client.account_information,
            "list_user_accounts",
            user_id=user_id,
            user_secret=user_secret,
        )

    def list_balances(self, user_id: str, user_secret: str, account_id: str) -> list[dict[str, Any]]:
        return _raw_rows(
            self.client.account_information,
            "get_user_account_balance",
            user_id=user_id,
            user_secret=user_secret,
            account_id=account_id,
        )

    def list_positions(self, user_id: str, user_secret: str, account_id: str) -> list[dict[str, Any]]:
        return _raw_rows(
            self.client.account_information,
            "get_user_account_positions",
            user_id=user_id,
            user_secret=user_secret,
            account_id=account_id,
        )

    def list_option_positions(
        self, user_id: str, user_secret: str, account_id: str,
    ) -> list[dict[str, Any]]:
        return _raw_rows(
            self.client.options,
            "list_option_holdings",
            user_id=user_id,
            user_secret=user_secret,
            account_id=account_id,
        )

    def list_activities(self, user_id: str, user_secret: str, account_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_activity_ids: set[str] = set()
        offset = 0
        expected_total: int | None = None
        for _ in range(_MAX_ACTIVITY_PAGES):
            payload = _raw_payload(
                self.client.account_information,
                "get_account_activities",
                user_id=user_id,
                user_secret=user_secret,
                account_id=account_id,
                offset=offset,
                limit=_ACTIVITY_PAGE_SIZE,
            )
            if not isinstance(payload, Mapping):
                raise RuntimeError("SnapTrade activities response 缺少 pagination")
            data = payload.get("data")
            if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
                raise RuntimeError("SnapTrade activities response data 格式錯誤")
            page = data
            pagination = payload.get("pagination")
            if not isinstance(pagination, Mapping):
                raise RuntimeError("SnapTrade activities response 缺少 pagination")
            page_offset = pagination.get("offset")
            page_limit = pagination.get("limit")
            total = pagination.get("total")
            if (
                type(page_offset) is not int
                or type(page_limit) is not int
                or type(total) is not int
                or page_offset != offset
                or page_limit < 1
                or page_limit < len(page)
                or total < offset + len(page)
            ):
                raise RuntimeError("SnapTrade activities pagination 格式錯誤")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise RuntimeError("SnapTrade activities pagination total 不一致")
            for row in page:
                activity_id = _text(row.get("id"))
                if not activity_id:
                    raise RuntimeError("SnapTrade activity 缺少 id")
                if activity_id in seen_activity_ids:
                    raise RuntimeError("SnapTrade activities 含重複 id")
                seen_activity_ids.add(activity_id)
            rows.extend(page)
            if len(rows) == expected_total:
                return rows
            if not page:
                raise RuntimeError("SnapTrade activities pagination 提前結束")
            offset += len(page)
        raise RuntimeError("SnapTrade activities 超過安全分頁上限；保留前次 snapshot")


class SnapTradeService:
    def __init__(self, gateway: Any | None = None) -> None:
        self.gateway = gateway

    def _require_configured(self) -> None:
        if not _configured():
            raise SnapTradeNotConfigured("SnapTrade server credentials 尚未設定")

    def _gateway(self) -> Any:
        self._require_configured()
        if self.gateway is None:
            self.gateway = SnapTradeSDKGateway()
        return self.gateway

    def _credentials(self, user_id: int) -> tuple[str, str] | None:
        row = db.snaptrade_get_credentials(user_id)
        if row is None:
            return None
        snaptrade_user_id, encrypted = row
        return snaptrade_user_id, _fernet().decrypt(encrypted).decode()

    def _ensure_credentials(self, user_id: int) -> tuple[str, str]:
        self._require_configured()
        existing = self._credentials(user_id)
        if existing:
            return existing
        with _operation_lock(user_id, "registration", 600) as lock_owner:
            existing = self._credentials(user_id)
            if existing:
                return existing
            requested_id = f"thoth-{user_id}"
            gateway = self._gateway()
            if requested_id in gateway.list_registered_user_ids():
                gateway.delete_user(requested_id)
            response = gateway.register_user(requested_id)
            snaptrade_user_id = _text(response.get("userId") or response.get("user_id"))
            secret = _text(response.get("userSecret") or response.get("user_secret"))
            if not snaptrade_user_id or not secret:
                raise RuntimeError("SnapTrade registerUser response 不完整")
            now = now_iso()
            try:
                stored = db.snaptrade_insert_credentials(
                    user_id,
                    snaptrade_user_id,
                    _fernet().encrypt(secret.encode()),
                    now,
                    lock_owner=lock_owner,
                )
            except Exception:
                try:
                    gateway.delete_user(snaptrade_user_id)
                except Exception as cleanup_error:
                    raise RuntimeError(
                        "SnapTrade 本機註冊失敗，且遠端清理失敗",
                    ) from cleanup_error
                raise
            if not stored:
                raise SnapTradeBusy(
                    "SnapTrade registration lease 已失效；請重新連線",
                )
            return snaptrade_user_id, secret

    def status(self, user_id: int) -> dict[str, Any]:
        configured = _configured()
        credentials = self._credentials(user_id) if configured else None
        connection_count: int | None = None
        if credentials:
            connection_count = len(self._gateway().list_connections(*credentials))
        return {
            "configured": configured,
            "registered": credentials is not None,
            "connection_count": connection_count,
            "last_synced_at": self.snapshot(user_id)["last_synced_at"],
        }

    def connection_url(self, user_id: int, redirect_uri: str) -> str:
        redirect_uri = _validate_redirect_uri(redirect_uri)
        credentials = self._ensure_credentials(user_id)
        return self._gateway().connection_url(*credentials, redirect_uri)

    def sync(self, user_id: int) -> dict[str, Any]:
        with _operation_lock(user_id, "sync", 600) as lock_owner:
            return self._sync_locked(user_id, lock_owner)

    def _sync_locked(self, user_id: int, lock_owner: str) -> dict[str, Any]:
        self._require_configured()
        credentials = self._credentials(user_id)
        if not credentials:
            raise SnapTradeNotRegistered("請先開啟 SnapTrade Connection Portal")
        gateway = self._gateway()
        connections = gateway.list_connections(*credentials)
        for connection in connections:
            disabled = connection.get("disabled")
            if type(disabled) is not bool:
                raise RuntimeError("SnapTrade connection 缺少 disabled 狀態")
            if disabled:
                raise RuntimeError("SnapTrade connection 已停用；保留前次 snapshot")
        authorization_slugs = {
            authorization_id: slug
            for connection in connections
            if (authorization_id := _text(connection.get("id")))
            if (raw_slug := _text(_nested(connection, "brokerage", "slug")))
            if (slug := raw_slug.upper())
        }
        upstream_accounts = _dedupe_accounts(gateway.list_accounts(*credentials))
        if connections and not upstream_accounts:
            raise RuntimeError("SnapTrade 有有效連線但帳戶回應為空；保留前次 snapshot")
        synced_at = now_iso()
        accounts: list[dict[str, Any]] = []
        balances: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        activities: list[dict[str, Any]] = []

        for raw in upstream_accounts:
            account_id = _text(raw.get("id"))
            institution = _text(raw.get("institution_name"))
            if not account_id or not institution:
                raise RuntimeError("SnapTrade account 缺少 id/institution_name")
            slug = _slug(raw, authorization_slugs)
            transactions_status = _nested(raw, "sync_status", "transactions")
            if not isinstance(transactions_status, Mapping):
                raise RuntimeError(
                    f"SnapTrade account {account_id} 缺少 transactions sync status",
                )
            activities_supported = True
            holdings_unavailable = _nested(
                raw, "sync_status", "holdings", "holdings_unavailable",
            ) is True
            if (
                not holdings_unavailable
                and _nested(raw, "sync_status", "holdings", "initial_sync_completed") is not True
            ):
                raise RuntimeError(
                    f"SnapTrade account {account_id} holdings 尚未完成初次同步",
                )
            if (
                activities_supported
                and transactions_status.get("initial_sync_completed") is not True
            ):
                raise RuntimeError(
                    f"SnapTrade account {account_id} transactions 尚未完成初次同步",
                )
            last_successful_sync = _text(transactions_status.get("last_successful_sync"))
            try:
                last_successful_date = date.fromisoformat(last_successful_sync or "")
            except ValueError as error:
                raise RuntimeError(
                    f"SnapTrade account {account_id} transactions freshness 格式錯誤",
                ) from error
            today = datetime.now(UTC).date()
            if (
                last_successful_date.isoformat() != last_successful_sync
                or last_successful_date > today
                or today - last_successful_date > _MAX_TRANSACTION_SYNC_LAG
            ):
                raise RuntimeError(
                    f"SnapTrade account {account_id} transactions freshness 已過期",
                )
            if holdings_unavailable:
                raw_balances = []
                raw_positions = []
            else:
                raw_balances = gateway.list_balances(*credentials, account_id)
                raw_positions = [
                    *gateway.list_positions(*credentials, account_id),
                    *gateway.list_option_positions(*credentials, account_id),
                ]
            raw_activities = (
                gateway.list_activities(*credentials, account_id)
                if activities_supported else []
            )
            mapped_balances = self._map_balances(account_id, raw_balances, synced_at)
            mapped_positions = self._map_positions(account_id, raw_positions, synced_at)
            mapped_activities = self._map_activities(account_id, raw_activities, synced_at)

            reported_total = _decimal(_nested(raw, "balance", "total", "amount"))
            account_currency = _text(_nested(raw, "balance", "total", "currency"))
            reported_cash = sum(
                (_decimal(balance["cash"]) or Decimal(0))
                for balance in mapped_balances
            )
            position_values = [_decimal(position["market_value"]) for position in mapped_positions]
            values_comparable = (
                account_currency is not None
                and all(balance["currency"] == account_currency for balance in mapped_balances)
                and all(position["currency"] == account_currency for position in mapped_positions)
                and all(value is not None for value in position_values)
            )
            if reported_total is not None and not holdings_unavailable and values_comparable:
                mapped_total = reported_cash + sum(
                    (value or Decimal(0)) for value in position_values
                )
                tolerance = max(Decimal("0.01"), abs(reported_total) * Decimal("0.001"))
                if abs(reported_total - mapped_total) > tolerance:
                    raise RuntimeError(
                        f"SnapTrade account {account_id} 回傳部分資料；保留前次 snapshot",
                    )
            accounts.append(self._map_account(raw, slug, activities_supported, synced_at))
            balances.extend(mapped_balances)
            positions.extend(mapped_positions)
            activities.extend(mapped_activities)

        self._replace_snapshot(
            user_id, accounts, balances, positions, activities, lock_owner,
        )
        return {
            "counts": {
                "accounts": len(accounts),
                "balances": len(balances),
                "positions": len(positions),
                "activities": len(activities),
            },
            "synced_at": synced_at,
        }

    @staticmethod
    def _map_account(raw: dict[str, Any], slug: str | None, activities_supported: bool, synced_at: str) -> dict[str, Any]:
        institution = _text(raw.get("institution_name")) or "Unknown"
        name = _text(raw.get("name")) or institution
        return {
            "id": str(raw["id"]),
            "name": name,
            "number": _text(raw.get("number")),
            "institution_name": institution,
            "brokerage_slug": slug,
            "balance_total": _decimal_text(_nested(raw, "balance", "total", "amount")),
            "balance_currency": _text(_nested(raw, "balance", "total", "currency")),
            "activities_supported": activities_supported,
            "holdings_unavailable": _nested(
                raw, "sync_status", "holdings", "holdings_unavailable",
            ) is True,
            "transactions_last_successful_sync": _text(
                _nested(raw, "sync_status", "transactions", "last_successful_sync"),
            ),
            "transactions_first_transaction_date": _text(
                _nested(raw, "sync_status", "transactions", "first_transaction_date"),
            ),
            "synced_at": synced_at,
        }

    @staticmethod
    def _map_balances(account_id: str, rows: list[dict[str, Any]], synced_at: str) -> list[dict[str, Any]]:
        out = []
        for row in rows:
            currency = _text(_nested(row, "currency", "code")) or _text(row.get("currency"))
            if not currency:
                raise RuntimeError("SnapTrade balance 缺少 currency")
            out.append({
                "account_id": account_id,
                "currency": currency,
                "cash": _decimal_text(row.get("cash")),
                "buying_power": _decimal_text(row.get("buying_power")),
                "synced_at": synced_at,
            })
        return out

    @staticmethod
    def _map_positions(account_id: str, rows: list[dict[str, Any]], synced_at: str) -> list[dict[str, Any]]:
        out = []
        for row in rows:
            symbol = (
                _text(_nested(row, "instrument", "symbol"))
                or _text(_nested(row, "symbol", "symbol", "symbol"))
                or _text(_nested(row, "symbol", "option_symbol", "ticker"))
            )
            if not symbol:
                raise RuntimeError("SnapTrade position 缺少 symbol")
            quantity = _decimal(row.get("units"))
            if quantity is None:
                quantity = _decimal(row.get("fractional_units"))
            if quantity is None:
                raise RuntimeError("SnapTrade position 缺少 units")
            if quantity == 0:
                continue
            price = _decimal(row.get("price"))
            average_cost = row.get("average_purchase_price")
            if average_cost is None:
                average_cost = row.get("cost_basis")
            symbol_id = (
                _text(_nested(row, "instrument", "id"))
                or _text(_nested(row, "symbol", "id"))
                or symbol
            )
            option_symbol = _nested(row, "symbol", "option_symbol")
            asset_type = (
                _text(_nested(row, "instrument", "kind"))
                or _text(_nested(row, "symbol", "symbol", "type", "code"))
                or (
                    "OPTION"
                    if isinstance(option_symbol, Mapping)
                    else None
                )
            )
            market_value = None
            if price is not None:
                if isinstance(option_symbol, Mapping):
                    multiplier = Decimal(10 if option_symbol.get("is_mini_option") is True else 100)
                    market_value = format(quantity * price * multiplier, "f")
                elif (asset_type or "").upper() not in {"OPTION", "OPTIONS"}:
                    market_value = format(quantity * price, "f")
            out.append({
                "account_id": account_id,
                "provider_symbol_id": symbol_id,
                "symbol": symbol,
                "description": (
                    _text(_nested(row, "instrument", "description"))
                    or _text(_nested(row, "symbol", "symbol", "description"))
                    or _text(_nested(row, "symbol", "description"))
                ),
                "asset_type": asset_type,
                "quantity": format(quantity, "f"),
                "price": format(price, "f") if price is not None else None,
                "market_value": market_value,
                "average_cost": _decimal_text(average_cost),
                "currency": (
                    _text(_nested(row, "currency", "code"))
                    or _text(row.get("currency"))
                    or _text(_nested(row, "symbol", "symbol", "currency", "code"))
                    or _text(
                        _nested(
                            row,
                            "symbol",
                            "option_symbol",
                            "underlying_symbol",
                            "currency",
                            "code",
                        ),
                    )
                ),
                "synced_at": synced_at,
            })
        return out

    @staticmethod
    def _map_activities(account_id: str, rows: list[dict[str, Any]], synced_at: str) -> list[dict[str, Any]]:
        out = []
        for row in rows:
            activity_id = _text(row.get("id"))
            activity_type = _text(row.get("type"))
            if not activity_id or not activity_type:
                raise RuntimeError("SnapTrade activity 缺少 id/type")
            raw_symbol = row.get("symbol")
            symbol_obj: Mapping[str, Any] = raw_symbol if isinstance(raw_symbol, Mapping) else {}
            symbol = _text(symbol_obj.get("symbol")) or _text(_nested(symbol_obj, "symbol", "symbol"))
            description = _text(row.get("description")) or _text(symbol_obj.get("description"))
            fee = _decimal_text(row.get("fee"))
            if fee is None:
                fee = _decimal_text(_nested(row, "fee", "amount"))
            out.append({
                "id": activity_id,
                "account_id": account_id,
                "type": activity_type,
                "trade_date": _text(row.get("trade_date")),
                "settlement_date": _text(row.get("settlement_date")),
                "symbol": symbol,
                "description": description,
                "units": _decimal_text(row.get("units")),
                "price": _decimal_text(row.get("price")),
                "amount": _decimal_text(row.get("amount")),
                "fee": fee,
                "currency": _text(_nested(row, "currency", "code")) or _text(row.get("currency")),
                "synced_at": synced_at,
            })
        return out

    @staticmethod
    def _replace_snapshot(
        user_id: int,
        accounts: list[dict[str, Any]],
        balances: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        activities: list[dict[str, Any]],
        lock_owner: str,
    ) -> None:
        if not db.snaptrade_replace_snapshot(
            user_id,
            accounts,
            balances,
            positions,
            activities,
            lock_owner=lock_owner,
            lock_now=now_iso(),
        ):
            raise SnapTradeBusy("SnapTrade sync lease 已失效；保留前次 snapshot")

    @staticmethod
    def snapshot(user_id: int) -> dict[str, Any]:
        return db.snaptrade_snapshot(user_id)
