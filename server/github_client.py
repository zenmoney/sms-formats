from typing import Any, Dict, Optional

from github import Auth, Github


class GitHubClient:
    def __init__(
        self,
        *,
        repo: str,
        token: Optional[str] = None,
        app_id: Optional[str] = None,
        installation_id: Optional[str] = None,
        private_key: Optional[str] = None,
        timeout_seconds: int = 30,
    ) -> None:
        if "/" not in repo:
            raise ValueError("GITHUB_REPO must be in owner/repo format.")
        self.repo = repo.strip()
        token_value = (token or "").strip()
        if token_value:
            self._token = token_value
            auth = Auth.Token(token_value)
            self._app_installation_auth = None
            self._client = Github(auth=auth, timeout=timeout_seconds)
            self._repo = self._client.get_repo(self.repo)
            return

        app_id_value = (app_id or "").strip()
        installation_id_value = (installation_id or "").strip()
        private_key_value = (private_key or "").strip()
        if not app_id_value or not installation_id_value or not private_key_value:
            raise ValueError(
                "Provide GITHUB_TOKEN or GITHUB_APP_ID + GITHUB_APP_INSTALLATION_ID + "
                "GITHUB_APP_PRIVATE_KEY."
            )
        normalized_private_key = private_key_value.replace("\\n", "\n")
        app_auth = Auth.AppAuth(
            app_id=int(app_id_value),
            private_key=normalized_private_key,
        )
        self._token = ""
        self._app_installation_auth = Auth.AppInstallationAuth(
            app_auth=app_auth,
            installation_id=int(installation_id_value),
        )
        self._client = Github(auth=self._app_installation_auth, timeout=timeout_seconds)
        self._repo = self._client.get_repo(self.repo)

    @property
    def token(self) -> str:
        if self._app_installation_auth is not None:
            return self._app_installation_auth.token
        return self._token

    def build_clone_url(self, repo: Optional[str] = None) -> str:
        target_repo = (repo or self.repo).strip()
        token_value = (self.token or "").strip()
        if token_value:
            return f"https://x-access-token:{token_value}@github.com/{target_repo}.git"
        return f"https://github.com/{target_repo}.git"

    async def find_open_issue_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        issues = self._repo.get_issues(state="open")
        for issue in issues:
            if issue.pull_request is not None:
                continue
            if issue.title == title:
                return {"number": issue.number, "title": issue.title}
        return None

    async def create_issue(self, title: str, body: str = "") -> Dict[str, Any]:
        issue = self._repo.create_issue(title=title, body=body or None)
        return {"number": issue.number, "title": issue.title}

    async def add_issue_comment(self, issue_number: int, body: str) -> Dict[str, Any]:
        issue = self._repo.get_issue(number=issue_number)
        comment = issue.create_comment(body=body)
        return {"id": comment.id}

    async def find_or_create_issue_and_comment(
        self, title: str, comment_body: str, issue_body: str = ""
    ) -> Dict[str, Any]:
        issue = await self.find_open_issue_by_title(title)
        if issue is None:
            issue = await self.create_issue(
                title=title,
                body=comment_body if comment_body else issue_body,
            )
            return issue
        issue_number = int(issue["number"])
        await self.add_issue_comment(issue_number=issue_number, body=comment_body)
        return issue

    async def find_open_pr(self, head_branch: str, base_branch: str) -> Optional[Dict[str, Any]]:
        owner = self.repo.split("/", 1)[0]
        heads = [head_branch, f"{owner}:{head_branch}"]
        for head in heads:
            pulls = self._repo.get_pulls(state="open", sort="created", base=base_branch, head=head)
            for pull in pulls:
                return {"number": pull.number, "title": pull.title}
        return None

    async def create_pr(
        self, title: str, body: str, head_branch: str, base_branch: str
    ) -> Dict[str, Any]:
        pull = self._repo.create_pull(
            title=title,
            body=body,
            head=head_branch,
            base=base_branch,
        )
        return {"number": pull.number, "title": pull.title}

    async def find_or_create_pr(
        self, *, title: str, body: str, head_branch: str, base_branch: str
    ) -> Dict[str, Any]:
        existing = await self.find_open_pr(head_branch=head_branch, base_branch=base_branch)
        if existing is not None:
            return existing
        return await self.create_pr(
            title=title,
            body=body,
            head_branch=head_branch,
            base_branch=base_branch,
        )
