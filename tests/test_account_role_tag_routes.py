from types import SimpleNamespace

from src.web.routes import accounts as accounts_routes


def test_account_to_response_includes_role_fields():
    account = SimpleNamespace(
        id=1,
        email="a@example.com",
        password="pwd",
        client_id="cid",
        email_service="manual",
        account_id="acc-1",
        workspace_id="ws-1",
        registered_at=None,
        last_refresh=None,
        expires_at=None,
        status="active",
        proxy_used=None,
        cpa_uploaded=False,
        cpa_uploaded_at=None,
        account_label="mother",
        role_tag="parent",
        biz_tag="team-a",
        pool_state="team_pool",
        pool_state_manual=None,
        priority=80,
        subscription_type="team",
        subscription_at=None,
        cookies=None,
        created_at=None,
        updated_at=None,
        extra_data={},
    )

    response = accounts_routes.account_to_response(account)

    assert response.account_label == "mother"
    assert response.role_tag == "parent"
    assert response.biz_tag == "team-a"
    assert response.pool_state == "team_pool"
    assert response.priority == 80


def test_resolve_account_ids_supports_role_tag_filter():
    class DummyColumn:
        def ilike(self, _pattern):
            return True

    class DummyQuery:
        def __init__(self):
            self.filters = []

        def filter(self, *args):
            self.filters.extend(args)
            return self

        def all(self):
            return [(1,), (2,)]

    class DummyDb:
        def query(self, _field):
            return DummyQuery()

    original_account = accounts_routes.Account
    accounts_routes.Account = SimpleNamespace(
        id=DummyColumn(),
        email=DummyColumn(),
        account_id=DummyColumn(),
        role_tag=DummyColumn(),
        email_service=DummyColumn(),
        status=DummyColumn(),
    )
    try:
        ids = accounts_routes.resolve_account_ids(
            DummyDb(),
            ids=[],
            select_all=True,
            role_tag_filter="parent",
        )
    finally:
        accounts_routes.Account = original_account

    assert ids == [1, 2]
