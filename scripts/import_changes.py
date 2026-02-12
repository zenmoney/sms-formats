#!/usr/bin/env python3
"""Import formats/senders from JSON on stdin: companies -> senders -> formats, with git commits."""

import json
import sys
from pathlib import Path

# Allow importing from same directory when run as script
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sms_format import (
    DeletedSmsFormat,
    SmsFormat,
    clean_name,
    get_format_name,
    validate_sms_format_for_import,
)
from sms_format_repository import (
    Company,
    find_company_by_id,
    find_format_by_id,
    save_company,
    save_format,
    save_senders,
    delete_format_by_id,
)


def fail(message):
    sys.stderr.write(message + "\n")
    sys.exit(1)


def run_git(command, env=None):
    import os
    import subprocess

    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        env=full_env,
    )
    if result.returncode != 0:
        fail(f"Git command failed: {command}\n{result.stderr or result.stdout}")
    return (result.stdout or "").strip()


def validate_changed(changed):
    from datetime import datetime, timezone

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
        fail(f"Invalid changed value: {changed}")


def commit_file(file_paths, message, changed):
    import subprocess

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
    quoted = " ".join(f'"{p}"' for p in relative_paths)
    run_git(f"git add -A --ignore-errors -- {quoted}", env=env)
    # Skip commit if nothing is staged (prevents hard failure on no-op updates).
    staged_check = subprocess.run(
        f"git diff --cached --quiet -- {quoted}",
        shell=True,
    )
    if staged_check.returncode == 0:
        return
    if staged_check.returncode not in (0, 1):
        fail("Failed to check staged changes before commit")
    safe_message = message.replace('"', '\\"')
    run_git(f'git commit -m "{safe_message}"', env=env)


def main():
    input_text = sys.stdin.read()
    if not input_text.strip():
        fail("No input provided on stdin")

    try:
        diff = json.loads(input_text)
    except json.JSONDecodeError as e:
        fail(f"Invalid JSON input: {e}")

    formats = diff.get("formats")
    if formats is None:
        formats = []
    elif not isinstance(formats, list):
        formats = []

    senders = diff.get("senders")
    if senders is None:
        senders = []
    elif not isinstance(senders, list):
        senders = []

    companies = diff.get("companies")
    if companies is None:
        companies = []
    elif not isinstance(companies, list):
        companies = []

    # First update companies
    for company in companies:
        company_id = company.get("id")
        name = clean_name(company.get("name") or "")
        changed = validate_changed(company.get("changed", ""))
        if company_id is None or not name:
            fail("Company entry missing id or name")

        company_result = save_company(Company(id=str(company_id), name=name))
        if company_result.changed_paths:
            action = "rename bank" if len(company_result.changed_paths) > 1 else "create bank"
            commit_file(company_result.changed_paths, f"[{name}] {action}", changed)

    # Then update senders
    for sender_entry in senders:
        company_id = sender_entry.get("companyId")
        senders_list = sender_entry.get("senders")
        if senders_list is None:
            senders_list = []
        elif not isinstance(senders_list, list):
            senders_list = []
        changed = validate_changed(sender_entry.get("changed", ""))
        if company_id is None:
            fail("Sender entry missing companyId")
        company = find_company_by_id(company_id)
        if not company:
            fail(f"Bank directory not found for companyId {company_id}")
        bank_name = company.name
        senders_result = save_senders(senders_list, str(company_id))
        if senders_result.changed_paths:
            commit_file(senders_result.changed_paths, f"[{bank_name}] update senders", changed)

    # Then update formats
    for format_entry in formats:
        has_regex = isinstance(format_entry.get("regexp"), str)
        has_examples = isinstance(format_entry.get("examples"), list)
        is_deletion = not has_regex and not has_examples

        if is_deletion:
            deleted = DeletedSmsFormat.from_diff_dict(format_entry)
            if not deleted.id:
                fail("Deleted format entry missing id")
            company_id = format_entry.get("companyId")
            try:
                existing = find_format_by_id(deleted.id, company_id)
            except ValueError as e:
                fail(str(e))
            if not existing:
                continue
            changed = validate_changed(deleted.changed)
            company = find_company_by_id(company_id) if company_id is not None else None
            bank_name = company.name if company else "unknown"
            delete_result = delete_format_by_id(deleted.id, company_id)
            if delete_result.changed_paths:
                commit_file(delete_result.changed_paths, f"[{bank_name}] delete format", changed)
            continue

        fmt = SmsFormat.from_diff_dict(format_entry)
        import_errors = validate_sms_format_for_import(fmt)
        if import_errors:
            fail(import_errors[0])
        changed = validate_changed(fmt.changed or "")
        name = get_format_name(fmt)
        company = find_company_by_id(fmt.company_id)
        if not company:
            fail(f"Bank directory not found for companyId {fmt.company_id}")
        bank_name = company.name
        id_part = str(fmt.id).strip() if fmt.id is not None else ""
        if name and id_part:
            stem = f"{name}_{id_part}"
        elif name:
            stem = name
        elif id_part:
            stem = f"_{id_part}"
        else:
            stem = "format"
        save_result = save_format(fmt, str(fmt.company_id), file_stem=stem)
        if save_result.changed_paths:
            commit_file(
                save_result.changed_paths,
                f"[{bank_name}] update format {name}",
                changed,
            )


if __name__ == "__main__":
    main()
