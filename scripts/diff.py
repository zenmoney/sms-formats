#!/usr/bin/env python3
"""Apply incoming diff and return repository diff since cursor."""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow importing from same directory when run as script
sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate as validate_script
from sms_format import (
    DeletedSmsFormat,
    SmsFormat,
    clean_name,
    get_format_name,
    validate_sms_format_for_import,
)
from sms_format_repository import (
    Company,
    delete_format_by_id,
    find_company_by_id,
    find_format_by_id,
    find_format_by_name,
    get_repo_root,
    list_senders,
    parse_name_with_id,
    save_company,
    save_format,
    save_senders,
)

COMMIT_HASH_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _run_git(args, env=None, return_result=False):
    import os

    git_args = list(args)
    if git_args and git_args[0] == "git":
        git_args = ["git", "-c", "core.quotepath=false", *git_args[1:]]
    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        git_args,
        capture_output=True,
        text=True,
        env=full_env,
        cwd=str(get_repo_root()),
    )
    if return_result:
        return result
    if result.returncode != 0:
        cmd = " ".join(args)
        raise RuntimeError(f"Git command failed: {cmd}\n{result.stderr or result.stdout}")
    return (result.stdout or "").strip()


def _commit_exists(commit_sha):
    if not commit_sha:
        return False
    if not COMMIT_HASH_RE.fullmatch(commit_sha):
        raise ValueError("Invalid lastCommitHash value")
    result = _run_git(["git", "cat-file", "-e", f"{commit_sha}^{{commit}}"], return_result=True)
    return result.returncode == 0


def _resolve_since_iso(since_value):
    try:
        as_number = int(float(str(since_value)))
        dt = datetime.utcfromtimestamp(as_number / 1000.0)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except (ValueError, OSError):
        pass
    try:
        dt = datetime.fromisoformat(str(since_value).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except (ValueError, TypeError):
        pass
    raise ValueError("Invalid lastServerTimestamp value (expected unix ms or ISO date)")


def _validate_changed(changed):
    try:
        s = str(changed).strip()
        if "Z" in s or "+" in s or s.count("-") >= 2:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except (ValueError, TypeError):
        raise ValueError(f"Invalid changed value: {changed}")


def commit_file(file_paths, message, changed):
    """Commit changes for provided paths using explicit commit timestamp."""
    env = {
        "GIT_AUTHOR_DATE": changed,
        "GIT_COMMITTER_DATE": changed,
    }
    paths = file_paths if isinstance(file_paths, list) else [file_paths]
    paths = [p for p in paths if p is not None]
    cwd = Path.cwd()
    relative_paths = []
    for p in paths:
        pp = Path(p)
        if not pp.is_absolute():
            pp = cwd / pp
        relative_paths.append(str(pp.relative_to(cwd)))
    _run_git(["git", "add", "-A", "--ignore-errors", "--", *relative_paths], env=env)
    staged_result = _run_git(
        ["git", "diff", "--cached", "--quiet", "--", *relative_paths],
        return_result=True,
    )
    staged_rc = staged_result.returncode
    if staged_rc == 0:
        return
    if staged_rc not in (0, 1):
        raise RuntimeError("Failed to check staged changes before commit")
    _run_git(["git", "commit", "-m", message], env=env)


def _parse_input():
    input_text = sys.stdin.read()
    if not input_text.strip():
        raise ValueError("No input provided on stdin")
    try:
        payload = json.loads(input_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON input: {e}") from e
    if not isinstance(payload, dict):
        raise ValueError("Input must be a JSON object")
    return payload


def _apply_import_diff(companies, senders, formats):
    for company in companies:
        company_id = company.get("id")
        name = clean_name(company.get("name") or "")
        changed = _validate_changed(company.get("changed", ""))
        if company_id is None or not name:
            raise ValueError("Company entry missing id or name")

        company_result = save_company(Company(id=str(company_id), name=name))
        if company_result.changed_paths:
            action = "rename bank" if len(company_result.changed_paths) > 1 else "create bank"
            commit_file(company_result.changed_paths, f"[{name}] {action}", changed)

    for sender_entry in senders:
        company_id = sender_entry.get("companyId")
        senders_list = sender_entry.get("senders")
        if senders_list is None or not isinstance(senders_list, list):
            senders_list = []
        changed = _validate_changed(sender_entry.get("changed", ""))
        if company_id is None:
            raise ValueError("Sender entry missing companyId")
        company = find_company_by_id(company_id)
        if not company:
            raise ValueError(f"Bank directory not found for companyId {company_id}")
        bank_name = company.name
        senders_result = save_senders(senders_list, str(company_id))
        if senders_result.changed_paths:
            commit_file(senders_result.changed_paths, f"[{bank_name}] update senders", changed)

    for format_entry in formats:
        has_regex = isinstance(format_entry.get("regexp"), str)
        has_examples = isinstance(format_entry.get("examples"), list)
        is_deletion = not has_regex and not has_examples

        if is_deletion:
            deleted = DeletedSmsFormat.from_diff_dict(format_entry)
            if not deleted.id:
                raise ValueError("Deleted format entry missing id")
            company_id = format_entry.get("companyId")
            existing = find_format_by_id(deleted.id, company_id)
            if not existing:
                continue
            changed = _validate_changed(deleted.changed)
            company = find_company_by_id(company_id) if company_id is not None else None
            bank_name = company.name if company else "unknown"
            delete_result = delete_format_by_id(deleted.id, company_id)
            if delete_result.changed_paths:
                commit_file(delete_result.changed_paths, f"[{bank_name}] delete format", changed)
            continue

        fmt = SmsFormat.from_diff_dict(format_entry)
        import_errors = validate_sms_format_for_import(fmt)
        if import_errors:
            raise ValueError(import_errors[0])
        if fmt.id is None or str(fmt.id).strip() == "":
            raise ValueError("Format entry missing id")
        changed = _validate_changed(fmt.changed or "")
        name = get_format_name(fmt)
        company = find_company_by_id(fmt.company_id)
        if not company:
            raise ValueError(f"Bank directory not found for companyId {fmt.company_id}")
        bank_name = company.name
        save_result = save_format(fmt, str(fmt.company_id))
        if save_result.changed_paths:
            commit_file(
                save_result.changed_paths,
                f"[{bank_name}] update format {name}",
                changed,
            )


def _get_last_change_iso(file_path):
    output = _run_git(["git", "log", "-1", "--format=%cI", "--", file_path])
    if not output:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return output.strip()


def _list_changes(last_commit_hash, last_server_timestamp):
    if last_commit_hash and _commit_exists(last_commit_hash):
        args = [
            "git",
            "log",
            f"{last_commit_hash}..HEAD",
            "--name-status",
            "--pretty=format:",
            "--",
            "src",
        ]
    else:
        if not last_server_timestamp:
            raise ValueError("Either valid lastCommitHash or lastServerTimestamp is required")
        since_iso = _resolve_since_iso(last_server_timestamp)
        args = [
            "git",
            "log",
            f"--since={since_iso}",
            "--name-status",
            "--pretty=format:",
            "--",
            "src",
        ]
    output = _run_git(args)
    if not output:
        return []
    latest_by_path = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("\t")]
        status = parts[0] if parts else ""
        if not status:
            continue
        if status.startswith("R"):
            if len(parts) < 3:
                continue
            path = parts[2]
        else:
            if len(parts) < 2:
                continue
            path = parts[1]
        if not path:
            continue
        if path not in latest_by_path:
            latest_by_path[path] = status
    return [{"status": status, "path": path} for path, status in latest_by_path.items()]


def _build_export_diff(changes):
    format_files = set()
    deleted_formats = []
    sender_files = set()

    for change in changes:
        path_str = change.get("path")
        if not path_str:
            continue
        parts = path_str.replace("\\", "/").split("/")
        if parts[0] != "src" or len(parts) < 3:
            continue
        bank_dir = parts[1]

        if parts[2] == "senders.txt":
            if change.get("status") != "D":
                sender_files.add(path_str)
            continue

        if len(parts) >= 4 and parts[2] == "formats":
            format_file = parts[3]
            if not format_file.endswith(".txt"):
                continue
            if change.get("status") == "D":
                base = format_file[:-4]
                format_id = parse_name_with_id(base)["id"]
                if format_id:
                    deleted_formats.append(
                        DeletedSmsFormat(
                            id=str(format_id),
                            changed=_get_last_change_iso(path_str),
                        ).to_diff_dict()
                    )
                continue
            format_files.add(path_str)

    formats_out = []
    for file_path in sorted(format_files):
        if not (Path.cwd() / file_path).exists():
            # A stale historical path can still appear in git log range;
            # export only the final state that exists in HEAD.
            continue
        parts = file_path.replace("\\", "/").split("/")
        if len(parts) < 4:
            continue
        bank_dir = parts[1]
        bank_id = parse_name_with_id(bank_dir)["id"]
        if not bank_id:
            continue
        format_file = parts[3]
        base = format_file[:-4]
        format_name = parse_name_with_id(base)["name"]
        format_id = parse_name_with_id(base)["id"]

        parsed = None
        if format_id is not None:
            parsed = find_format_by_id(format_id, str(bank_id))
        if not parsed:
            parsed = find_format_by_name(format_name, str(bank_id))
        if not parsed:
            raise ValueError(f"Format not found for companyId={bank_id}, name={format_name}")

        parsed.name = format_name
        parsed.id = format_id
        parsed.company_id = str(bank_id)
        parsed.changed = _get_last_change_iso(file_path)
        formats_out.append(parsed.to_diff_dict())

    senders_out = []
    for file_path in sorted(sender_files):
        if not (Path.cwd() / file_path).exists():
            continue
        parts = file_path.replace("\\", "/").split("/")
        if len(parts) < 3:
            continue
        bank_dir = parts[1]
        bank_id = parse_name_with_id(bank_dir)["id"]
        if not bank_id:
            continue
        senders_list = list_senders(str(bank_id))
        senders_out.append(
            {
                "companyId": str(bank_id),
                "changed": _get_last_change_iso(file_path),
                "senders": senders_list,
            }
        )

    formats_sorted = sorted(
        formats_out + deleted_formats,
        key=lambda x: (
            str(x.get("companyId", "")),
            str(x.get("id", "")),
            str(x.get("changed", "")),
        ),
    )
    senders_sorted = sorted(
        senders_out,
        key=lambda x: (
            str(x.get("companyId", "")),
            str(x.get("changed", "")),
        ),
    )

    return {
        "formats": formats_sorted,
        "senders": senders_sorted,
    }


def _get_head_commit_hash():
    return _run_git(["git", "rev-parse", "HEAD"])


def _current_changed_timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _normalize_list(value):
    if value is None or not isinstance(value, list):
        return []
    return value


def _normalize_diff_payload(changes):
    payload_diff = changes if isinstance(changes, dict) else {}
    return (
        _normalize_list(payload_diff.get("companies")),
        _normalize_list(payload_diff.get("senders")),
        _normalize_list(payload_diff.get("formats")),
    )


def _normalize_cursor(last_commit_hash, last_server_timestamp):
    normalized_commit = str(last_commit_hash).strip() if last_commit_hash is not None else None
    if not normalized_commit:
        normalized_commit = None
    normalized_since = (
        str(last_server_timestamp).strip() if last_server_timestamp is not None else None
    )
    if not normalized_since:
        normalized_since = None
    return normalized_commit, normalized_since


def _format_validation_errors(errors):
    base = Path.cwd()
    return "\n".join(validate_script._format_error_line(err, base) for err in errors)


def _run_validation_with_fix_and_commit():
    errors = validate_script.validate(fix=True)
    if errors:
        raise ValueError(_format_validation_errors(errors))
    # Commit only if validation fixes actually changed files.
    commit_file(["src"], "[sync] auto-fix validation", _current_changed_timestamp())


def diff(changes, last_commit_hash=None, last_server_timestamp=None):
    """Apply incoming diff and return resulting repository diff and HEAD commit."""
    companies, senders, formats = _normalize_diff_payload(changes)
    last_commit_hash, last_server_timestamp = _normalize_cursor(
        last_commit_hash, last_server_timestamp
    )

    _apply_import_diff(companies, senders, formats)
    _run_validation_with_fix_and_commit()
    changed_paths = _list_changes(last_commit_hash, last_server_timestamp)

    return {
        "diff": _build_export_diff(changed_paths),
        "commitHash": _get_head_commit_hash(),
    }


def main():
    try:
        payload = _parse_input()
        response = diff(
            changes=payload.get("diff"),
            last_commit_hash=payload.get("lastCommitHash"),
            last_server_timestamp=payload.get("lastServerTimestamp"),
        )
        sys.stdout.write(json.dumps(response, indent=2, ensure_ascii=False) + "\n")
    except Exception as exc:
        sys.stderr.write(str(exc) + "\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
