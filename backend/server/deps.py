"""FastAPI 共用 dependencies。

放這裡（不放 app.py）避免 routers 反向 import app 造成循環。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

# auto_error=False：自己擋，回 401 而不是 fastapi 預設的 403
oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def current_user(token: str | None = Depends(oauth2)) -> dict:
    """Auth dependency：解 JWT → 撈 user dict；失敗一律 401。

    auth / users 用 lazy import — conftest 每 test 會 reload 那兩個 module，
    top-level import 會抓到舊 class（AuthError 比對失敗）。
    """
    from backend.server.auth import AuthError, decode_access_token
    from backend.server.users import get_user_by_id

    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "尚未登入")
    try:
        claims = decode_access_token(token)
    except AuthError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from None
    user = get_user_by_id(int(claims["sub"]))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "找不到使用者")
    return user
