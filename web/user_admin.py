"""Administrator role management; authorization and history share the account transaction."""
import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from .auth import AuthError, AdminRequired


def install(app, tier, user_scope_key):
    def actor(request):
        user = request.scope.get(user_scope_key) or {}
        if not user.get("is_admin"):
            raise AdminRequired("仅管理员可管理用户。")
        return user["email"]

    def error(exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, exc.status)

    @app.get("/api/admin/users")
    async def directory(request: Request):
        try:
            return await asyncio.to_thread(tier.auth.store.directory, actor(request))
        except AuthError as exc:
            return error(exc)

    @app.post("/api/admin/users/role")
    async def change_role(request: Request):
        try:
            operator = actor(request)
            try:
                body = await request.json()
            except ValueError:
                raise AuthError("请求必须为有效 JSON。")
            if not isinstance(body, dict) or not isinstance(body.get("email"), str):
                raise AuthError("请选择已注册用户。")
            return await asyncio.to_thread(
                tier.auth.store.set_admin, operator, body["email"],
                body.get("is_admin"), body.get("expected_admin"),
            )
        except AuthError as exc:
            return error(exc)

    async def password_body(request):
        try:
            body = await request.json()
        except ValueError:
            raise AuthError("请求必须为有效 JSON。")
        if not isinstance(body, dict) or not isinstance(body.get("current_password"), str):
            raise AuthError("请填写当前密码。")
        return body

    @app.post("/api/auth/password")
    async def change_password(request: Request):
        from .auth import SESSION_COOKIE
        try:
            body = await password_body(request)
            user = request.scope[user_scope_key]
            claims = tier.auth.signer.claims(request.cookies.get(SESSION_COOKIE))
            await asyncio.to_thread(tier.auth.store.change_password, user["email"], user["email"],
                                    body["current_password"], body.get("new_password"), claims["ver"])
            response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
            response.delete_cookie(SESSION_COOKIE, path="/")
            return response
        except AuthError as exc:
            return error(exc)

    @app.post("/api/admin/users/reset-password")
    async def reset_password(request: Request):
        import secrets
        from .auth import SESSION_COOKIE
        try:
            operator = actor(request)
            body = await password_body(request)
            if not isinstance(body.get("email"), str):
                raise AuthError("请选择已注册用户。")
            claims = tier.auth.signer.claims(request.cookies.get(SESSION_COOKIE))
            password = secrets.token_urlsafe(18)
            user = await asyncio.to_thread(tier.auth.store.change_password, operator, body["email"],
                                           body["current_password"], password, claims["ver"], True)
            return JSONResponse({"user": user, "temporary_password": password, "expires_in": 86400},
                                headers={"Cache-Control": "no-store"})
        except AuthError as exc:
            return error(exc)
