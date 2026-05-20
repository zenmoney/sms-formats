#!/usr/bin/env python3
"""Validate format files: names, columns, regex match, group count, no cross-match."""

import argparse
import re
import sys
from pathlib import Path

from sms_format import (
    SmsFormat,
    ValidationError,
    clean_name,
    compile_regex,
    validate_cross_match,
    validate_sms_format,
)
from sms_format_repository import (
    Company,
    find_format_by_name,
    get_src_dir,
    list_companies,
    list_formats_with_files_and_errors,
    parse_name_with_id,
    save_company,
    save_format,
)


def _is_format_file_path(file_path):
    """Check if file is a valid format file path (in /formats/ with .txt extension)."""
    return str(file_path).endswith(".txt") and "/formats/" in str(file_path)


def _check_file_extensions(src_dir: Path) -> list[ValidationError]:
    """
    Check for files with invalid extensions/names in src/ directory structure.
    Rules:
    - In /formats/: only *.txt allowed
    - In bank root (src/Bank_X/): only senders.txt allowed
    - Any other files: error
    """
    errors = []
    for file_path in src_dir.rglob("*"):
        if not file_path.is_file():
            continue
        path_str = str(file_path)
        parts = file_path.parts
        # Check if file is in a /formats/ directory
        if "/formats/" in path_str:
            if not path_str.endswith(".txt"):
                errors.append(
                    ValidationError(
                        kind="invalid_extension",
                        file_path=str(file_path),
                        message="Invalid file extension (only .txt allowed)",
                    )
                )
        else:
            # File is not in /formats/, check if it's in bank root
            # Bank root is direct child of src/
            if "src" in parts:
                src_idx = parts.index("src")
                # Check if file is directly in src/Bank_XXX/ (not in subdirs except formats)
                if src_idx + 3 < len(parts):
                    # File is in a subdirectory other than /formats/
                    subdir = parts[src_idx + 2]
                    if subdir != "formats":
                        errors.append(
                            ValidationError(
                                kind="invalid_file",
                                file_path=str(file_path),
                                message="Invalid file (only senders.txt allowed in bank root)",
                            )
                        )
                elif src_idx + 1 < len(parts):
                    # File is in bank root (src/Bank_XXX/filename)
                    filename = file_path.name
                    if filename != "senders.txt":
                        errors.append(
                            ValidationError(
                                kind="invalid_file",
                                file_path=str(file_path),
                                message="Invalid file (only senders.txt allowed in bank root)",
                            )
                        )
    return errors


def _check_format_filename_chars(src_dir: Path) -> list[ValidationError]:
    """
    Check that format filenames in /formats/ contain only allowed characters.
    Allowed: letters (any alphabet), digits, spaces, underscore (_).
    No hidden characters or special symbols.
    """
    errors = []
    # Pattern: one or more allowed chars
    # \w matches letters, digits, underscore (including Unicode letters).
    pattern = re.compile(r"[\w ]+")
    for file_path in src_dir.rglob("*"):
        if not file_path.is_file():
            continue
        if "/formats/" not in str(file_path):
            continue
        filename = file_path.name
        if not pattern.match(filename):
            errors.append(
                ValidationError(
                    kind="invalid_filename_chars",
                    file_path=str(file_path),
                    message=(
                        "Invalid characters in filename "
                        "(only letters, digits, spaces, underscore allowed)"
                    ),
                )
            )
    return errors


def _check_bank_folder_names(src_dir: Path) -> list[ValidationError]:
    """
    Check that bank folder names follow the Name_ID format.
    Example: Simbank-kg_16076
    """
    errors = []
    for bank_dir in src_dir.iterdir():
        if not bank_dir.is_dir():
            continue
        parsed = parse_name_with_id(bank_dir.name)
        if parsed["id"] is None:
            errors.append(
                ValidationError(
                    kind="invalid_name",
                    file_path=str(bank_dir),
                    message="Invalid bank folder name (expected: Name_ID, e.g., Simbank-kg_16076)",
                )
            )
    return errors


def _relative_path(path, base=None):
    """Path relative to base (default cwd) for shorter output."""
    base = base or Path.cwd()
    try:
        return Path(path).resolve().relative_to(Path(base).resolve())
    except ValueError:
        return path


def _format_error_line(err: ValidationError, base=None) -> str:
    """Single line for stderr from a ValidationError (path: message style)."""
    path = _relative_path(err.file_path, base) if err.file_path else ""
    if path and not err.message.startswith(str(path)):
        return f"{path}: {err.message}"
    return err.message


def _print_errors(errors, src_dir, stream):
    """Print errors in test-runner style: header, one line per error, summary."""
    if not errors:
        return
    base = Path.cwd()
    stream.write("Validation FAILED\n")
    stream.write("=" * 60 + "\n")
    for err in errors:
        stream.write(_format_error_line(err, base) + "\n")
    files_with_errors = len({e.file_path for e in errors})
    stream.write("=" * 60 + "\n")
    stream.write(f"{len(errors)} error(s) in {files_with_errors} file(s)\n")


def _company_id_from_path(file_path: str):
    parts = Path(file_path).parts
    if "src" not in parts:
        return None
    idx = parts.index("src")
    if idx + 1 >= len(parts):
        return None
    company_dir = parts[idx + 1]
    return parse_name_with_id(company_dir)["id"]


def _format_name_and_id_from_path(file_path: str):
    stem = Path(file_path).stem
    parsed = parse_name_with_id(stem)
    return parsed["name"], parsed["id"], stem


def _collect_validation_errors():
    """Full pass over all banks and format files."""
    errors = []
    src_dir = get_src_dir()
    # Проверка расширений и имён файлов
    errors.extend(_check_file_extensions(src_dir))
    # Проверка символов в именах файлов форматов
    errors.extend(_check_format_filename_chars(src_dir))
    # Проверка имён папок банков
    errors.extend(_check_bank_folder_names(src_dir))
    companies = list_companies()
    for company in companies:
        bank_dir_name = f"{company.name}_{company.id}" if company.id is not None else company.name
        bank_path = src_dir / bank_dir_name
        bank_name = company.name
        if bank_name != clean_name(bank_name):
            errors.append(
                ValidationError(
                    kind="invalid_name",
                    file_path=str(bank_path),
                    message="Invalid bank dir name",
                    expected_name=clean_name(bank_name),
                )
            )
        format_records, parse_errors = list_formats_with_files_and_errors(company.id)
        errors.extend(parse_errors)
        if not format_records:
            continue
        formats = []
        formats_with_regex = []
        for parsed, file_path in format_records:
            try:
                compiled = compile_regex(parsed.regex, file_path)
                format_name = parsed.name or ""
                formats.append((file_path, format_name, parsed, compiled))
                formats_with_regex.append((parsed, compiled, file_path))
            except ValidationError as e:
                errors.append(e)
            except Exception:
                errors.append(
                    ValidationError(
                        kind="invalid_format",
                        file_path=file_path,
                        message="Invalid format file",
                    )
                )
        for file_path, format_name, parsed, compiled in formats:
            errors.extend(
                validate_sms_format(
                    parsed,
                    file_path=file_path,
                    format_name=format_name,
                    compiled_regex=compiled,
                )
            )
        errors.extend(validate_cross_match(formats_with_regex))
    return errors


def _apply_validation_fixes(errors):
    """
    Apply fixable corrections: delete invalid_format files; remove example_no_match and
    cross_match examples; rename format files and bank dirs for invalid_name.
    Bank renames are done last so format paths stay valid.
    """
    to_delete = set()
    to_remove_examples = {}
    format_renames = []
    bank_renames = []
    format_rename_target = {}
    for err in errors:
        if err.kind == "invalid_format":
            to_delete.add(err.file_path)
        elif err.kind in ("invalid_extension", "invalid_file", "invalid_filename_chars"):
            to_delete.add(err.file_path)
        elif err.kind in ("example_no_match", "cross_match") and err.example_text is not None:
            to_remove_examples.setdefault(err.file_path, set()).add(err.example_text)
        elif err.kind == "invalid_name" and err.expected_name:
            if _is_format_file_path(err.file_path):
                path = Path(err.file_path)
                stem = path.stem
                parsed = parse_name_with_id(stem)
                id_part = parsed["id"]
                new_stem = (
                    f"{err.expected_name}_{id_part}" if id_part is not None else err.expected_name
                )
                new_path = path.parent / f"{new_stem}.txt"
                if str(new_path) != err.file_path:
                    format_rename_target[err.file_path] = str(new_path)
            else:
                bank_renames.append((err.file_path, err.expected_name))
    format_renames = list(format_rename_target.items())
    for file_path in to_delete:
        path_obj = Path(file_path)
        if path_obj.exists():
            path_obj.unlink()
    for file_path, remove_set in to_remove_examples.items():
        if file_path in to_delete:
            continue
        company_id = _company_id_from_path(file_path)
        format_name, format_id, old_stem = _format_name_and_id_from_path(file_path)
        if company_id is None:
            continue
        parsed = find_format_by_name(format_name, str(company_id))
        if not parsed:
            continue
        kept = [ex for ex in parsed.examples if ex not in remove_set]
        if not kept:
            path_obj = Path(file_path)
            if path_obj.exists():
                path_obj.unlink()
        else:
            updated = SmsFormat(
                regex=parsed.regex,
                regex_group_names=list(parsed.regex_group_names),
                examples=kept,
                name=parsed.name,
                id=parsed.id,
                company_id=parsed.company_id,
                changed=parsed.changed,
            )
            save_format(updated, str(company_id), file_stem=old_stem)
    for old_path, new_path in format_renames:
        if old_path == new_path:
            continue
        company_id = _company_id_from_path(old_path)
        if company_id is None:
            continue
        old_name, _old_id, _old_stem = _format_name_and_id_from_path(old_path)
        new_stem = Path(new_path).stem
        parsed = find_format_by_name(old_name, str(company_id))
        if not parsed:
            continue
        save_format(parsed, str(company_id), file_stem=new_stem)
        old_file = Path(old_path)
        if old_file.exists():
            old_file.unlink()
    for bank_path_str, expected_name in bank_renames:
        company_id = parse_name_with_id(Path(bank_path_str).name)["id"]
        if company_id is None:
            continue
        save_company(Company(id=str(company_id), name=expected_name))


def validate(fix: bool = False) -> list[ValidationError]:
    """Validate repository formats and optionally apply auto-fixes."""
    errors = _collect_validation_errors()
    if fix and errors:
        _apply_validation_fixes(errors)
        errors = _collect_validation_errors()
    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate SMS format files.")
    parser.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Fix what can be fixed: delete invalid format files, "
            "remove invalid examples, rename format/bank to expected name. "
            "Bank renames applied last."
        ),
    )
    args = parser.parse_args()
    src_dir = get_src_dir()
    if not src_dir.exists():
        sys.stderr.write("No src/ directory found.\n")
        sys.exit(1)
    companies = list_companies()
    if not companies:
        sys.stderr.write("No banks found in src/\n")
        sys.exit(1)
    errors = validate(fix=args.fix)
    if errors:
        _print_errors(errors, src_dir, sys.stderr)
        sys.exit(1)
    sys.stdout.write("Validation OK\n")


if __name__ == "__main__":
    main()
