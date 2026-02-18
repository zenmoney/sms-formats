import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .github_client import GitHubClient


@dataclass
class GenerationOutcome:
    status: str
    reason: str
    commit_title: Optional[str] = None


def _run(cmd, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        check=False,
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({' '.join(cmd)}): {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def _parse_generator_output(result: subprocess.CompletedProcess) -> GenerationOutcome:
    stdout = (result.stdout or "").strip()
    if result.returncode != 0:
        return GenerationOutcome(
            status="failed",
            reason=(result.stderr or "generator_failed").strip(),
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return GenerationOutcome(
            status="failed",
            reason=f"invalid_generator_output: {stdout}",
        )
    status = str(payload.get("status") or "failed")
    commit_title_raw = payload.get("commit_title")
    commit_title = commit_title_raw if isinstance(commit_title_raw, str) else None
    return GenerationOutcome(
        status=status,
        reason=str(payload.get("reason") or ""),
        commit_title=commit_title,
    )


def run_generation_flow(
    *,
    github_client: GitHubClient,
    github_repo: str,
    base_branch: str,
    company_id: str,
    sms_text: str,
) -> Tuple[str, Optional[str], Optional[str]]:
    branch_name = f"company-{company_id}"
    with tempfile.TemporaryDirectory(prefix="sms-webhook-") as temp_dir:
        tmp_root = Path(temp_dir)
        repo_path = tmp_root / "repo"
        print(f"repo path {repo_path}")
        clone_url = github_client.build_clone_url(github_repo)
        _run(["git", "clone", clone_url, str(repo_path)], cwd=tmp_root)

        remote_branch_check = _run(
            ["git", "ls-remote", "--heads", "origin", branch_name],
            cwd=repo_path,
            check=False,
        )
        has_remote_branch = bool((remote_branch_check.stdout or "").strip())
        if has_remote_branch:
            _run(["git", "fetch", "origin", branch_name], cwd=repo_path)
            _run(["git", "checkout", "-B", branch_name, f"origin/{branch_name}"], cwd=repo_path)
        else:
            _run(["git", "checkout", base_branch], cwd=repo_path)
            _run(["git", "checkout", "-b", branch_name], cwd=repo_path)

        python_bin = os.environ.get("PYTHON_BIN", "python3")
        generator_run = subprocess.run(
            [
                python_bin,
                "scripts/generate_sms_format.py",
                "--save",
                "--company",
                company_id,
            ],
            cwd=str(repo_path),
            check=False,
            text=True,
            # capture_output=True,
            input=sms_text,
        )
        outcome = _parse_generator_output(generator_run)
        if outcome.status == "duplicate":
            return "duplicate", None, None
        if outcome.status in {"otp", "transaction", "failed_transaction"}:
            fresh_push_url = github_client.build_clone_url(github_repo)
            _run(["git", "remote", "set-url", "origin", fresh_push_url], cwd=repo_path)
            _run(["git", "push", "-u", "origin", branch_name], cwd=repo_path)
            return outcome.status, branch_name, outcome.commit_title
        return "failed", None, None


def clean_issue_suffix(text: str) -> str:
    if not isinstance(text, str):
        return ""
    cleaned = text.replace("\n", " ").replace("\r", " ").strip()
    for symbol in ["'", '"', "$", "/", "\\", ".", "{", "}", "_", "(", ")"]:
        cleaned = cleaned.replace(symbol, " ")
    cleaned = " ".join(cleaned.split())
    return cleaned[:80]


async def process_known_company_sms(
    *,
    github_client: GitHubClient,
    github_repo: str,
    github_base_branch: str,
    company_id: str,
    company_name: str,
    sender: str,
    text: str,
) -> str:
    status, branch_name, commit_title = run_generation_flow(
        github_client=github_client,
        github_repo=github_repo,
        base_branch=github_base_branch,
        company_id=company_id,
        sms_text=text,
    )
    if status in {"otp", "transaction", "failed_transaction"} and branch_name:
        message = f"Sender:\n{sender}\n\nText:\n{text}"
        await github_client.find_or_create_pr(
            title=commit_title or f"[{company_name}] create format",
            body=message,
            head_branch=branch_name,
            base_branch=github_base_branch,
        )
        return status

    if status == "duplicate":
        return status

    issue_title = f"Unknown format for {company_name}: {clean_issue_suffix(text)}"
    message = f"Sender:\n{sender}\n\nText:\n{text}"
    await github_client.find_or_create_issue_and_comment(
        title=issue_title,
        comment_body=message,
        issue_body="Automatic report from SMS webhook server.",
    )
    return "failed"
