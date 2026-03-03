import asyncio
import os
from contextlib import asynccontextmanager
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .github_client import GitHubClient
from .models import DiffRequest, DiffResponse, SmsRequest, SmsResponse
from .repo_worker import process_known_company_sms, run_diff_flow


class KeyedExecutionQueue:
    def __init__(self) -> None:
        self._state_lock = asyncio.Lock()
        self._locks: Dict[str, asyncio.Lock] = {}
        self._users: Dict[str, int] = {}

    @asynccontextmanager
    async def acquire(self, key: str):
        async with self._state_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
                self._users[key] = 0
            self._users[key] += 1

        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            async with self._state_lock:
                self._users[key] -= 1
                if self._users[key] <= 0 and not lock.locked():
                    self._users.pop(key, None)
                    self._locks.pop(key, None)


app = FastAPI(title="sms-formats-webhook")
queue = KeyedExecutionQueue()
_github_client: Optional[GitHubClient] = None


def _get_github_client() -> GitHubClient:
    global _github_client
    if _github_client is not None:
        return _github_client
    github_repo = os.environ.get("GITHUB_REPO", "").strip()
    if not github_repo:
        raise RuntimeError("Missing required environment variable: GITHUB_REPO")
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID", "").strip()
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "").strip()
    if github_token:
        _github_client = GitHubClient(
            repo=github_repo,
            token=github_token,
        )
    else:
        if not app_id:
            raise RuntimeError("Missing required environment variable: GITHUB_APP_ID")
        if not installation_id:
            raise RuntimeError("Missing required environment variable: GITHUB_APP_INSTALLATION_ID")
        if not private_key:
            raise RuntimeError("Missing required environment variable: GITHUB_APP_PRIVATE_KEY")
        _github_client = GitHubClient(
            repo=github_repo,
            app_id=app_id,
            installation_id=installation_id,
            private_key=private_key,
        )
    return _github_client


def _build_serialization_key(sms: SmsRequest) -> str:
    if sms.sms.company_id:
        return sms.sms.company_id.strip()
    company_name = sms.sms.company_name.strip()
    sender = sms.sms.sender.strip()
    return f"{company_name}_{sender}"


def _sms_report(sender: str, text: str) -> str:
    return f"Sender:\n{sender}\n\nText:\n{text}"


@app.post("/process-sms/", response_model=SmsResponse)
async def ingest_sms(payload: SmsRequest):
    try:
        github_client = _get_github_client()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    github_repo = github_client.repo
    base_branch = os.environ.get("GITHUB_BASE_BRANCH", "main").strip() or "main"

    key = _build_serialization_key(payload)
    async with queue.acquire(key):
        if not payload.sms.company_id:
            title = f"Unknown sender for {payload.sms.company_name}"
            await github_client.find_or_create_issue(
                title=title,
                issue_body=_sms_report(payload.sms.sender, payload.sms.text),
            )
            return SmsResponse(status="unknown_sender")

        status = await process_known_company_sms(
            github_client=github_client,
            github_repo=github_repo,
            github_base_branch=base_branch,
            company_id=payload.sms.company_id,
            company_name=payload.sms.company_name,
            sender=payload.sms.sender,
            text=payload.sms.text,
        )
        if status == "duplicate":
            return JSONResponse(
                status_code=409,
                content=SmsResponse(status="duplicate").model_dump(),
            )
        return SmsResponse(status=status)


@app.post("/diff/", response_model=DiffResponse)
async def ingest_diff(payload: DiffRequest):
    try:
        github_client = _get_github_client()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    github_repo = github_client.repo
    base_branch = os.environ.get("GITHUB_BASE_BRANCH", "main").strip() or "main"

    async with queue.acquire("diff-main"):
        try:
            result = run_diff_flow(
                github_client=github_client,
                github_repo=github_repo,
                base_branch=base_branch,
                payload=payload.model_dump(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            detail = str(exc)
            if detail.startswith("invalid_diff_output:"):
                raise HTTPException(status_code=500, detail=detail) from exc
            raise HTTPException(status_code=400, detail=detail) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result
