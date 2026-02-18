import asyncio

from fastapi.testclient import TestClient

from server import app as app_module


class FakeGitHubClient:
    created = []
    inits = []

    def __init__(
        self,
        *,
        repo: str,
        token: str = "",
        app_id: str = "",
        installation_id: str = "",
        private_key: str = "",
        timeout_seconds: int = 30,
    ) -> None:
        self.token = token or "app-token"
        self.repo = repo
        self.timeout_seconds = timeout_seconds
        self.inits.append(
            {
                "repo": repo,
                "token": token,
                "app_id": app_id,
                "installation_id": installation_id,
                "private_key": private_key,
            }
        )

    async def find_or_create_issue_and_comment(
        self, title: str, comment_body: str, issue_body: str = ""
    ):
        self.created.append((title, comment_body, issue_body))
        return {"number": 1, "title": title}


def test_unknown_sender_returns_unknown_sender(monkeypatch):
    monkeypatch.setattr(app_module, "_github_client", None)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    monkeypatch.setattr(app_module, "GitHubClient", FakeGitHubClient)

    payload = {
        "sms": {
            "company_name": "Some Bank",
            "sender": "BANK",
            "text": "Code 1234",
        }
    }
    with TestClient(app_module.app) as client:
        response = client.post("/process-sms/", json=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "unknown_sender"}
    assert FakeGitHubClient.created
    assert FakeGitHubClient.created[-1][0] == "Unknown sender for Some Bank"


def test_unknown_sender_uses_github_app_credentials_when_token_missing(monkeypatch):
    monkeypatch.setattr(app_module, "_github_client", None)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "test-key")
    monkeypatch.setattr(app_module, "GitHubClient", FakeGitHubClient)

    payload = {
        "sms": {
            "company_name": "Some Bank",
            "sender": "BANK",
            "text": "Code 1234",
        }
    }
    with TestClient(app_module.app) as client:
        response = client.post("/process-sms/", json=payload)

    assert response.status_code == 200
    assert FakeGitHubClient.inits[-1]["app_id"] == "123"
    assert FakeGitHubClient.inits[-1]["installation_id"] == "456"


def test_unknown_sender_accepts_bank_name_fallback(monkeypatch):
    monkeypatch.setattr(app_module, "_github_client", None)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    monkeypatch.setattr(app_module, "GitHubClient", FakeGitHubClient)

    payload = {
        "sms": {
            "bank_name": "Alias Bank",
            "sender": "BANK",
            "text": "Code 5678",
        }
    }
    with TestClient(app_module.app) as client:
        response = client.post("/process-sms/", json=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "unknown_sender"}
    assert FakeGitHubClient.created[-1][0] == "Unknown sender for Alias Bank"


def test_known_company_statuses(monkeypatch):
    monkeypatch.setattr(app_module, "_github_client", None)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    monkeypatch.setattr(app_module, "GitHubClient", FakeGitHubClient)

    statuses = ["duplicate", "transaction", "failed"]
    for expected_status in statuses:

        async def _fake_process_known_company_sms(**kwargs):
            return expected_status

        monkeypatch.setattr(
            app_module,
            "process_known_company_sms",
            _fake_process_known_company_sms,
        )
        payload = {
            "sms": {
                "company_name": "Known Bank",
                "sender": "BANK",
                "text": "Payment 100",
                "company_id": "123",
            }
        }
        with TestClient(app_module.app) as client:
            response = client.post("/process-sms/", json=payload)
        expected_code = 409 if expected_status == "duplicate" else 200
        assert response.status_code == expected_code
        assert response.json() == {"status": expected_status}


def test_queue_serializes_same_key_tasks():
    queue = app_module.KeyedExecutionQueue()
    events = []

    async def _task(name: str, delay: float):
        async with queue.acquire("same-key"):
            events.append(f"{name}:start")
            await asyncio.sleep(delay)
            events.append(f"{name}:end")

    async def _run():
        t1 = asyncio.create_task(_task("first", 0.05))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(_task("second", 0.0))
        await asyncio.gather(t1, t2)

    asyncio.run(_run())
    assert events == ["first:start", "first:end", "second:start", "second:end"]
