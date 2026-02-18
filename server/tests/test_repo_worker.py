import asyncio

from server import repo_worker


class FakeGitHubClient:
    def __init__(self) -> None:
        self.pr_calls = []

    def build_clone_url(self, repo):
        return f"https://github.com/{repo}.git"

    async def find_or_create_pr(self, *, title, body, head_branch, base_branch):
        self.pr_calls.append(
            {
                "title": title,
                "body": body,
                "head_branch": head_branch,
                "base_branch": base_branch,
            }
        )
        return {"number": 1}

    async def find_or_create_issue_and_comment(self, title, comment_body, issue_body=""):
        return {"number": 2}


def test_process_known_company_sms_uses_commit_title_and_sender_text(monkeypatch):
    def _fake_run_generation_flow(**kwargs):
        return "transaction", "company-123", "[Bank] create format"

    monkeypatch.setattr(repo_worker, "run_generation_flow", _fake_run_generation_flow)
    fake_client = FakeGitHubClient()

    status = asyncio.run(
        repo_worker.process_known_company_sms(
            github_client=fake_client,
            github_repo="owner/repo",
            github_base_branch="main",
            company_id="123",
            company_name="Bank",
            sender="BANK",
            text="Payment 100",
        )
    )

    assert status == "transaction"
    assert fake_client.pr_calls
    call = fake_client.pr_calls[0]
    assert call["title"] == "[Bank] create format"
    assert call["body"] == "Sender:\nBANK\n\nText:\nPayment 100"


def test_process_known_company_sms_fallback_title_when_commit_title_missing(monkeypatch):
    def _fake_run_generation_flow(**kwargs):
        return "otp", "company-123", None

    monkeypatch.setattr(repo_worker, "run_generation_flow", _fake_run_generation_flow)
    fake_client = FakeGitHubClient()

    status = asyncio.run(
        repo_worker.process_known_company_sms(
            github_client=fake_client,
            github_repo="owner/repo",
            github_base_branch="main",
            company_id="123",
            company_name="Bank",
            sender="BANK",
            text="OTP 1111",
        )
    )

    assert status == "otp"
    assert fake_client.pr_calls[0]["title"] == "[Bank] create format"
