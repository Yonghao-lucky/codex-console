from types import SimpleNamespace

from src.web.routes import registration


def test_normalize_email_lookup_value_handles_case_and_spaces():
    assert registration._normalize_email_lookup_value(" Test@Outlook.com ") == "test@outlook.com"


def test_find_registered_account_by_email_uses_normalized_match(monkeypatch):
    expected = SimpleNamespace(id=42, email="test@outlook.com")

    class DummyQuery:
        def filter(self, *_args):
            return self

        def first(self):
            return expected

    class DummyDb:
        def query(self, _model):
            return DummyQuery()

    result = registration._find_registered_account_by_email(DummyDb(), " Test@Outlook.com ")

    assert result is expected
