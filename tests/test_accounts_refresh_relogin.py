from types import SimpleNamespace

from src.web.routes import accounts as accounts_routes


def test_single_refresh_falls_back_to_relogin(monkeypatch):
    monkeypatch.setattr(accounts_routes, "_get_proxy", lambda proxy=None: proxy)
    monkeypatch.setattr(
        accounts_routes,
        "do_refresh",
        lambda account_id, proxy: SimpleNamespace(success=False, error_message="refresh failed", expires_at=None),
    )
    monkeypatch.setattr(accounts_routes, "reconcile_account_runtime_state", lambda account_id, proxy_url=None: {"success": True})
    monkeypatch.setattr(
        accounts_routes,
        "_attempt_relogin_session_refresh",
        lambda account_id, proxy: {"success": True, "reconciled": {"subscription_type": "team"}},
    )

    result = accounts_routes.asyncio.run(
        accounts_routes.refresh_account_token(1, accounts_routes.TokenRefreshRequest(relogin_if_needed=True))
    )

    assert result["success"] is True
    assert "重新登录" in result["message"]
    assert result["reconciled"]["subscription_type"] == "team"
