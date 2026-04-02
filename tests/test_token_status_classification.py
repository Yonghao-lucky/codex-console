from types import SimpleNamespace

from src.config.constants import AccountStatus
from src.core.openai import token_refresh


def test_quota_limited_error_is_expired_not_failed():
    assert token_refresh._is_quota_limited_error("usage limit reached") is True
    assert token_refresh._is_quota_limited_error("hourly quota exhausted") is True
    assert token_refresh._is_quota_limited_error("5小时限额已用完") is True


def test_network_error_is_not_failed(monkeypatch):
    updates = []

    class DummyDb:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    account = SimpleNamespace(id=1, access_token="token", status=AccountStatus.ACTIVE.value)

    monkeypatch.setattr(token_refresh, "get_db", lambda: DummyDb())
    monkeypatch.setattr(token_refresh.crud, "get_account_by_id", lambda db, account_id: account)
    monkeypatch.setattr(token_refresh.crud, "update_account", lambda db, account_id, **kwargs: updates.append(kwargs))

    class DummyManager:
        def __init__(self, proxy_url=None):
            pass

        def validate_token(self, access_token, timeout_seconds=30):
            return False, "验证异常: timeout while connecting"

    monkeypatch.setattr(token_refresh, "TokenRefreshManager", DummyManager)

    is_valid, error = token_refresh.validate_account_token(1)

    assert is_valid is False
    assert "timeout" in error.lower()
    assert updates == []


def test_subscription_limited_error_maps_to_expired(monkeypatch):
    updates = []

    class DummyDb:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    account = SimpleNamespace(id=1, access_token="token", status=AccountStatus.ACTIVE.value)

    monkeypatch.setattr(token_refresh, "get_db", lambda: DummyDb())
    monkeypatch.setattr(token_refresh.crud, "get_account_by_id", lambda db, account_id: account)
    monkeypatch.setattr(token_refresh.crud, "update_account", lambda db, account_id, **kwargs: updates.append(kwargs))

    class DummyManager:
        def __init__(self, proxy_url=None):
            pass

        def validate_token(self, access_token, timeout_seconds=30):
            return False, "usage limit reached"

    monkeypatch.setattr(token_refresh, "TokenRefreshManager", DummyManager)

    is_valid, _error = token_refresh.validate_account_token(1)

    assert is_valid is False
    assert updates == [{"status": AccountStatus.EXPIRED.value}]
