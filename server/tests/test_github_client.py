import asyncio

from server.github_client import GitHubClient


def test_find_or_create_issue_writes_message_to_body_on_create(monkeypatch):
    client = object.__new__(GitHubClient)
    calls = []

    async def _find_open_issue_by_title(title):
        return None

    async def _create_issue(title, body=""):
        calls.append(("create_issue", title, body))
        return {"number": 123, "title": title}

    async def _add_issue_comment(issue_number, body):
        calls.append(("add_issue_comment", issue_number, body))
        return {"id": 1}

    monkeypatch.setattr(client, "find_open_issue_by_title", _find_open_issue_by_title)
    monkeypatch.setattr(client, "create_issue", _create_issue)
    monkeypatch.setattr(client, "add_issue_comment", _add_issue_comment)

    issue = asyncio.run(
        client.find_or_create_issue(
            title="Unknown sender for Bank",
            issue_body="Sender:\nSENDER\n\nText:\ntext",
        )
    )

    assert issue["number"] == 123
    assert calls == [
        (
            "create_issue",
            "Unknown sender for Bank",
            "Sender:\nSENDER\n\nText:\ntext",
        )
    ]


def test_find_or_create_issue_adds_comment_when_issue_exists(monkeypatch):
    client = object.__new__(GitHubClient)
    calls = []

    async def _find_open_issue_by_title(title):
        return {"number": 77, "title": title}

    async def _create_issue(title, body=""):
        calls.append(("create_issue", title, body))
        return {"number": 123}

    async def _add_issue_comment(issue_number, body):
        calls.append(("add_issue_comment", issue_number, body))
        return {"id": 1}

    monkeypatch.setattr(client, "find_open_issue_by_title", _find_open_issue_by_title)
    monkeypatch.setattr(client, "create_issue", _create_issue)
    monkeypatch.setattr(client, "add_issue_comment", _add_issue_comment)

    issue = asyncio.run(
        client.find_or_create_issue(
            title="Unknown format for Bank: msg",
            issue_body="Sender:\nSENDER\n\nText:\ntext",
        )
    )

    assert issue["number"] == 77
    assert calls == [("add_issue_comment", 77, "Sender:\nSENDER\n\nText:\ntext")]
