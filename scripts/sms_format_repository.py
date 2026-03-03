#!/usr/bin/env python3
"""Repository API for companies, senders and SMS formats."""

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Iterator, Optional, TypeVar, Union

from sms_format import (
    MARKER_COLUMNS,
    MARKER_EXAMPLE,
    SmsFormat,
    ValidationError,
    clean_name,
    get_format_name,
)


@dataclass
class Company:
    id: Optional[str]
    name: str


T = TypeVar("T")


@dataclass
class ChangeResult(Generic[T]):
    changed_paths: list[str]
    entity: Optional[T]


def get_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def get_src_dir() -> Path:
    return get_repo_root() / "src"


def parse_name_with_id(raw: str) -> dict[str, Optional[str]]:
    last_underscore = raw.rfind("_")
    if last_underscore == -1:
        return {"name": raw, "id": None}
    name = raw[:last_underscore]
    id_part = raw[last_underscore + 1 :]
    if id_part == "":
        return {"name": name, "id": None}
    return {"name": name, "id": id_part}


def _company_dir(company: Company) -> Path:
    if company.id is None or str(company.id) == "":
        return get_src_dir() / company.name
    return get_src_dir() / f"{company.name}_{company.id}"


def _iter_company_dirs() -> Iterator[Path]:
    src_dir = get_src_dir()
    if not src_dir.exists():
        return
    for d in src_dir.iterdir():
        if d.is_dir():
            yield d


def _company_from_dir_name(dir_name: str) -> Company:
    parsed = parse_name_with_id(dir_name)
    cid = parsed["id"]
    return Company(id=str(cid) if cid is not None else None, name=str(parsed["name"]))


def list_companies() -> list[Company]:
    return [_company_from_dir_name(d.name) for d in _iter_company_dirs()]


def find_company_by_id(company_id: Union[str, int, None]) -> Optional[Company]:
    if company_id is None:
        return None
    target = str(company_id)
    for d in _iter_company_dirs():
        company = _company_from_dir_name(d.name)
        if str(company.id) == target:
            return company
    return None


def find_company_by_name(name: str) -> Optional[Company]:
    for d in _iter_company_dirs():
        company = _company_from_dir_name(d.name)
        if company.name == name:
            return company
    return None


def save_company(company: Company) -> ChangeResult[Company]:
    if not company.name:
        raise ValueError("Company name is required")

    id = str(company.id) if company.id is not None else None
    name = clean_name(company.name)
    company = Company(id=id, name=name)
    path = _company_dir(company)

    existing = find_company_by_id(id) if id is not None else None
    if not existing:
        by_name = find_company_by_name(name)
        if by_name and ((by_name.id is None and id is not None) or (by_name.id == id)):
            existing = by_name
    if existing:
        existing_path = _company_dir(existing)
        if existing_path == path:
            return ChangeResult(changed_paths=[], entity=company)
        if path.exists():
            raise ValueError(f"Company directory already exists: {path}")
        existing_path.rename(path)
        return ChangeResult(changed_paths=[str(existing_path), str(path)], entity=company)

    path.mkdir(parents=True, exist_ok=True)
    senders_path = path / "senders.txt"
    if not senders_path.exists():
        senders_path.write_text("", encoding="utf-8")
    return ChangeResult(changed_paths=[str(path)], entity=company)


def _read_senders_file(file_path: Path) -> list[str]:
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    lines = content.splitlines()
    return [line.strip() for line in lines if line.strip()]


def list_senders(company_id: Union[str, int]) -> list[str]:
    company = find_company_by_id(company_id)
    if not company:
        return []
    file_path = _company_dir(company) / "senders.txt"
    if not file_path.exists():
        return []
    return _read_senders_file(file_path)


def save_senders(senders: list[str], company_id: Union[str, int]) -> ChangeResult[list[str]]:
    company = find_company_by_id(company_id)
    if not company:
        raise ValueError(f"Company not found for id {company_id}")
    file_path = _company_dir(company) / "senders.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(senders) + "\n" if senders else "\n"
    file_path.write_text(content, encoding="utf-8")
    return ChangeResult(changed_paths=[str(file_path)], entity=senders)


def _iter_format_files_for_company(company: Company) -> Iterator[Path]:
    formats_dir = _company_dir(company) / "formats"
    if not formats_dir.exists():
        return
    for f in formats_dir.iterdir():
        if f.is_file() and f.name.endswith(".txt"):
            yield formats_dir / f.name


def _parse_format_file(file_path: Union[str, Path]) -> SmsFormat:
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    lines = content.splitlines()
    if not lines:
        raise ValidationError(
            kind="invalid_format", file_path=file_path, message="Invalid format file: missing regex"
        )
    regex_line = lines[0].strip() if lines[0] else ""
    if not regex_line:
        raise ValidationError(
            kind="invalid_format", file_path=file_path, message="Invalid format file: missing regex"
        )

    i = 1
    if i >= len(lines) or lines[i].strip() != "":
        raise ValidationError(
            kind="invalid_format",
            file_path=file_path,
            message=f"Invalid format file: expected empty line before {MARKER_COLUMNS}",
        )
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or lines[i].strip() != MARKER_COLUMNS:
        raise ValidationError(
            kind="invalid_format",
            file_path=file_path,
            message=f"Invalid format file: missing {MARKER_COLUMNS} section",
        )
    i += 1
    if i >= len(lines):
        raise ValidationError(
            kind="invalid_format",
            file_path=file_path,
            message="Invalid format file: missing columns line",
        )
    columns_line = lines[i].strip()
    columns = [c.strip() for c in columns_line.split(";")] if columns_line else []
    i += 1

    examples = []
    if i >= len(lines) or lines[i].strip() != "":
        raise ValidationError(
            kind="invalid_format",
            file_path=file_path,
            message=f"Invalid format file: expected empty line before {MARKER_EXAMPLE}",
        )
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    while i < len(lines):
        if lines[i].strip() != MARKER_EXAMPLE:
            raise ValidationError(
                kind="invalid_format",
                file_path=file_path,
                message=f"Invalid format file: expected {MARKER_EXAMPLE}",
            )
        i += 1
        example_lines = []
        while i < len(lines) and lines[i].strip() != MARKER_EXAMPLE:
            example_lines.append(lines[i])
            i += 1
        if i < len(lines) and example_lines and example_lines[-1].strip() != "":
            raise ValidationError(
                kind="invalid_format",
                file_path=file_path,
                message=f"Invalid format file: expected empty line before {MARKER_EXAMPLE}",
            )
        example_text = "\n".join(example_lines)
        if not example_text.strip():
            raise ValidationError(
                kind="invalid_format",
                file_path=file_path,
                message="Invalid format file: empty example",
            )
        examples.append(example_text)

    if not examples:
        raise ValidationError(
            kind="invalid_format",
            file_path=file_path,
            message="Invalid format file: no examples",
        )

    return SmsFormat(
        regex=regex_line,
        regex_group_names=columns,
        examples=examples,
    )


def _write_format_file_content(format: SmsFormat, examples: Optional[list[str]] = None) -> str:
    examples = examples if examples is not None else format.examples
    if not examples:
        raise ValueError("Cannot write format file with no examples")
    regex_line = format.regex
    columns_line = ";".join(format.regex_group_names)
    blocks = [
        regex_line.strip(),
        MARKER_COLUMNS + "\n" + (columns_line.strip() if columns_line else ""),
    ]
    for ex in examples:
        blocks.append(MARKER_EXAMPLE + "\n" + (ex.strip() if ex else ""))
    content = "\n\n".join(blocks) + "\n"
    return content


def _save_format_file(
    file_path: Union[str, Path], format: SmsFormat, examples: Optional[list[str]] = None
) -> bool:
    content = _write_format_file_content(format, examples=examples)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def list_formats_with_files(
    company_id: Union[str, int], include_invalid: bool = False
) -> list[tuple[SmsFormat, str]]:
    company = find_company_by_id(company_id)
    if not company:
        return []
    items = []
    for file_path in _iter_format_files_for_company(company):
        base_name = file_path.stem
        parsed_name = parse_name_with_id(base_name)
        try:
            fmt = _parse_format_file(file_path)
            fmt.name = parsed_name["name"]
            fmt.id = parsed_name["id"]
            fmt.company_id = str(company.id) if company.id is not None else None
        except ValidationError:
            if not include_invalid:
                continue
            raise
        items.append((fmt, str(file_path)))
    return items


def list_formats_with_files_and_errors(
    company_id: Union[str, int],
) -> tuple[list[tuple[SmsFormat, str]], list[ValidationError]]:
    company = find_company_by_id(company_id)
    if not company:
        return [], []
    items = []
    errors = []
    for file_path in _iter_format_files_for_company(company):
        base_name = file_path.stem
        parsed_name = parse_name_with_id(base_name)
        try:
            fmt = _parse_format_file(file_path)
            fmt.name = parsed_name["name"]
            fmt.id = parsed_name["id"]
            fmt.company_id = str(company.id) if company.id is not None else None
            items.append((fmt, str(file_path)))
        except ValidationError as e:
            errors.append(e)
    return items, errors


def list_formats(company_id: Union[str, int]) -> list[SmsFormat]:
    return [fmt for fmt, _file_path in list_formats_with_files(company_id)]


def _load_format_from_file(file_path: Path, company: Company) -> SmsFormat:
    parsed_name = parse_name_with_id(file_path.stem)
    fmt = _parse_format_file(file_path)
    fmt.name = str(parsed_name["name"])
    fmt.id = parsed_name["id"]
    fmt.company_id = str(company.id) if company.id is not None else None
    return fmt


def find_format_by_id(
    format_id: Union[str, int], company_id: Optional[Union[str, int]] = None
) -> Optional[SmsFormat]:
    target = str(format_id)
    matches: list[tuple[Path, Company]] = []
    companies = [find_company_by_id(company_id)] if company_id is not None else list_companies()
    for company in companies:
        if company is None:
            continue
        for file_path in _iter_format_files_for_company(company):
            stem_info = parse_name_with_id(file_path.stem)
            if str(stem_info["id"]) == target:
                matches.append((file_path, company))
                if company_id is not None and len(matches) > 1:
                    break
    if len(matches) > 1:
        raise ValueError(f"Ambiguous format id {format_id}: multiple files found")
    if not matches:
        return None
    file_path, company = matches[0]
    return _load_format_from_file(file_path, company)


def find_format_by_name(name: str, company_id: Union[str, int]) -> Optional[SmsFormat]:
    company = find_company_by_id(company_id)
    if company is None:
        return None
    for file_path in _iter_format_files_for_company(company):
        stem_info = parse_name_with_id(file_path.stem)
        if str(stem_info["name"]) == name:
            return _load_format_from_file(file_path, company)
    return None


def save_format(
    format: SmsFormat, company_id: Union[str, int], file_stem: Optional[str] = None
) -> ChangeResult[SmsFormat]:
    company = find_company_by_id(company_id)
    if not company:
        raise ValueError(f"Company not found for id {company_id}")
    formats_dir = _company_dir(company) / "formats"
    formats_dir.mkdir(parents=True, exist_ok=True)

    id = str(format.id).strip() if format.id is not None else ""
    name = clean_name(get_format_name(format) or "")

    if file_stem:
        stem = file_stem
    else:
        if name and id:
            stem = f"{name}_{id}"
        elif name:
            stem = name
        elif id:
            # Explicit leading underscore only when we have id but no name.
            stem = f"_{id}"
        else:
            raise ValueError(
                f"Cannot determine file name stem for format without name and id ${format}"
            )
    file_path = formats_dir / f"{stem}.txt"
    changed_paths: list[str] = []

    duplicate_paths: list[Path] = []
    if id:
        for other_path in _iter_format_files_for_company(company):
            if other_path == file_path:
                continue
            parsed_name = parse_name_with_id(other_path.stem)
            if str(parsed_name["id"]) == id or (
                not parsed_name["id"] and parsed_name["name"] == name
            ):
                duplicate_paths.append(other_path)

    changed = _save_format_file(file_path, format)
    if changed:
        changed_paths.append(str(file_path))

    for p in duplicate_paths:
        if p.exists():
            p.unlink()
            changed_paths.append(str(p))

    return ChangeResult(changed_paths=changed_paths, entity=format)


def delete_format_by_id(
    format_id: Union[str, int], company_id: Optional[Union[str, int]] = None
) -> ChangeResult[None]:
    target = str(format_id)
    companies = [find_company_by_id(company_id)] if company_id is not None else list_companies()
    matches: list[Path] = []
    for company in companies:
        if company is None:
            continue
        for file_path in _iter_format_files_for_company(company):
            stem_info = parse_name_with_id(file_path.stem)
            if str(stem_info["id"]) == target:
                matches.append(file_path)
    if len(matches) > 1:
        raise ValueError(f"Ambiguous format id {format_id}: multiple files found")
    if not matches:
        return ChangeResult(changed_paths=[], entity=None)
    path = matches[0]
    if path.exists():
        path.unlink()
    return ChangeResult(changed_paths=[str(path)], entity=None)


def delete_format_by_name(name: str, company_id: Union[str, int]) -> ChangeResult[None]:
    company = find_company_by_id(company_id)
    if company is None:
        return ChangeResult(changed_paths=[], entity=None)
    matches = []
    for file_path in _iter_format_files_for_company(company):
        stem_info = parse_name_with_id(file_path.stem)
        if str(stem_info["name"]) == name:
            matches.append(file_path)
    if len(matches) > 1:
        raise ValueError(f"Ambiguous format name {name}: multiple files found")
    if not matches:
        return ChangeResult(changed_paths=[], entity=None)
    path = matches[0]
    if path.exists():
        path.unlink()
    return ChangeResult(changed_paths=[str(path)], entity=None)
