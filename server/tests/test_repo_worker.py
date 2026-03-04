import asyncio
from subprocess import CompletedProcess

from server import repo_worker


class FakeGitHubClient:
    def __init__(self) -> None:
        self.pr_calls = []

    def build_clone_url(self, repo):
        return f"https://github.com/{repo}.git"

    async def find_or_create_pr(self, *, title, body, head_branch, base_branch, draft=False):
        self.pr_calls.append(
            {
                "title": title,
                "body": body,
                "head_branch": head_branch,
                "base_branch": base_branch,
                "draft": bool(draft),
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
    assert call["draft"] is False


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
    assert fake_client.pr_calls[0]["draft"] is False


def test_process_known_company_sms_marks_draft_pr(monkeypatch):
    def _fake_run_generation_flow(**kwargs):
        return "transaction_draft", "company-123", "[Bank] create format draft"

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

    assert status == "transaction_draft"
    assert fake_client.pr_calls
    assert fake_client.pr_calls[0]["title"] == "[Bank] create format draft"
    assert fake_client.pr_calls[0]["draft"] is True


def test_run_diff_flow_success(monkeypatch):
    commands = []

    def _fake_run(cmd, cwd, check=True):
        commands.append(cmd)
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    def _fake_subprocess_run(*args, **kwargs):
        return CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"diff":{"companies":[],"senders":[],"formats":[]},"commitHash":"abc123"}',
            stderr="",
        )

    monkeypatch.setattr(repo_worker, "_run", _fake_run)
    monkeypatch.setattr(repo_worker.subprocess, "run", _fake_subprocess_run)

    result = repo_worker.run_diff_flow(
        github_client=FakeGitHubClient(),
        github_repo="owner/repo",
        base_branch="main",
        payload={"diff": {"companies": [], "senders": [], "formats": []}, "lastCommitHash": "abc"},
    )

    assert result["commitHash"] == "abc123"
    assert ["git", "push", "origin", "HEAD:main"] in commands


def test_run_diff_flow_invalid_output(monkeypatch):
    def _fake_run(cmd, cwd, check=True):
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    def _fake_subprocess_run(*args, **kwargs):
        return CompletedProcess(args=args[0], returncode=0, stdout="not-json", stderr="")

    monkeypatch.setattr(repo_worker, "_run", _fake_run)
    monkeypatch.setattr(repo_worker.subprocess, "run", _fake_subprocess_run)

    try:
        repo_worker.run_diff_flow(
            github_client=FakeGitHubClient(),
            github_repo="owner/repo",
            base_branch="main",
            payload={"diff": {"companies": [], "senders": [], "formats": []}},
        )
        assert False, "Expected RuntimeError for invalid diff output"
    except RuntimeError as exc:
        assert "invalid_diff_output" in str(exc)


def test_run_diff_flow_nonzero_exit(monkeypatch):
    def _fake_run(cmd, cwd, check=True):
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    def _fake_subprocess_run(*args, **kwargs):
        return CompletedProcess(args=args[0], returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(repo_worker, "_run", _fake_run)
    monkeypatch.setattr(repo_worker.subprocess, "run", _fake_subprocess_run)

    try:
        repo_worker.run_diff_flow(
            github_client=FakeGitHubClient(),
            github_repo="owner/repo",
            base_branch="main",
            payload={"diff": {"companies": [], "senders": [], "formats": []}},
        )
        assert False, "Expected RuntimeError for failed diff run"
    except RuntimeError as exc:
        assert "diff.py failed" in str(exc)
