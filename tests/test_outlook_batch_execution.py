from src.web.routes import registration


class DummyBackgroundTasks:
    def __init__(self):
        self.calls = []

    def add_task(self, func, *args):
        self.calls.append((func, args))


def test_outlook_batch_schedule_passes_registration_type(monkeypatch):
    captured = {}

    def fake_schedule(background_tasks, coroutine_func, *args):
        captured["func"] = coroutine_func
        captured["args"] = args

    class DummyQuery:
        def filter(self, *_args):
            return self

        def first(self):
            return type("Svc", (), {"config": {"email": "a@example.com"}, "name": "a@example.com"})()

    class DummyDb:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def query(self, _model):
            return DummyQuery()

    monkeypatch.setattr(registration, "_schedule_async_job", fake_schedule)
    monkeypatch.setattr(registration, "get_db", lambda: DummyDb())
    monkeypatch.setattr(registration, "_find_registered_account_by_email", lambda db, email: None)

    req = registration.OutlookBatchRegistrationRequest(
        service_ids=[1],
        registration_type="parent",
    )

    result = registration.asyncio.run(registration._start_outlook_batch_registration_internal(req, DummyBackgroundTasks()))

    assert result.to_register == 1
    assert captured["func"] is registration.run_outlook_batch_registration
    assert captured["args"][-1] == "parent"
