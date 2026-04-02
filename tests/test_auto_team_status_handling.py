from types import SimpleNamespace

from src.config.constants import AccountStatus
from src.web.routes import auto_team as auto_team_routes


def test_compute_team_status_treats_expired_as_active_for_console():
    status = auto_team_routes._compute_team_status(AccountStatus.EXPIRED.value, 1, 6)
    assert status == AccountStatus.ACTIVE.value


def test_build_console_row_keeps_free_plan_without_workspace_candidates(monkeypatch):
    account = SimpleNamespace(
        id=1,
        email="team@example.com",
        status=AccountStatus.ACTIVE.value,
        access_token="",
        subscription_type="free",
        account_id="",
        workspace_id="",
        role_tag="parent",
        account_label="mother",
        priority=50,
        last_used_at=None,
        updated_at=None,
        last_refresh=None,
        extra_data={},
    )

    row = auto_team_routes._build_console_row_for_account(
        account=account,
        proxy_url=None,
        include_member_counts=False,
        request_timeout_seconds=5,
    )

    assert row["plan"] == "free"


def test_cached_verify_does_not_force_realtime_on_http_403():
    assert auto_team_routes._cached_verify_needs_realtime("workspace_candidates_http_403") is False
    assert auto_team_routes._cached_verify_needs_realtime("workspace_candidates_http_401") is True
