#!/usr/bin/env python3
"""Parser for format files and senders."""

import re
from dataclasses import dataclass
from typing import List, Optional, Union

MARKER_COLUMNS = "-----COLUMNS-----"
MARKER_EXAMPLE = "-----EXAMPLE-----"
_INVALID_FILENAME_CHARS_RE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')
_WINDOWS_RESERVED_BASENAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
}

ALLOWED_COLUMNS = {
    "payee",
    "income",
    "outcome",
    "fee",
    "cashback",
    "op_income",
    "op_outcome",
    "balance",
    "comment",
    "av_balance",
    "instrument",
    "op_instrument",
    "acc_instrument",
    "date",
    "syncid",
    "mcc",
}

AMOUNT_COLUMNS = {
    "income",
    "outcome",
    "fee",
    "cashback",
    "op_income",
    "op_outcome",
    "balance",
    "av_balance",
}


def normalize_column_name(column):
    """Return base column name (before #) for validation."""
    return column.split("#")[0].strip()


@dataclass
class SmsFormat:
    regex: str
    regex_group_names: list
    examples: list
    name: Optional[str] = None
    id: Optional[Union[int, str]] = None
    company_id: Optional[str] = None
    changed: Optional[str] = None

    def to_diff_dict(self):
        return {
            "id": self.id,
            "companyId": self.company_id,
            "changed": self.changed,
            "name": self.name,
            "regexp": self.regex.strip(),
            "regexpGroupNames": [c.strip() for c in self.regex_group_names],
            "examples": [ex.strip() for ex in self.examples],
        }

    @classmethod
    def from_diff_dict(cls, d):
        regex = d.get("regexp")
        if isinstance(regex, str):
            regex = _clean_text(regex)
        else:
            regex = ""
        names = d.get("regexpGroupNames")
        if names is None:
            names = []
        elif isinstance(names, str):
            names = [n.strip() for n in names.strip().split(";")] if names else []
        else:
            names = [str(n).strip() for n in names]
        examples = d.get("examples")
        if not isinstance(examples, list):
            examples = []
        return cls(
            regex=regex,
            regex_group_names=names,
            examples=examples,
            name=d.get("name"),
            id=d.get("id"),
            company_id=d.get("companyId"),
            changed=d.get("changed"),
        )


@dataclass
class DeletedSmsFormat:
    id: str
    changed: str

    def to_diff_dict(self):
        return {"id": self.id, "changed": self.changed}

    @classmethod
    def from_diff_dict(cls, d):
        return cls(
            id=str(d.get("id", "")),
            changed=str(d.get("changed", "")),
        )


def clean_name(name):
    if not isinstance(name, str):
        return ""
    s = re.sub(r"[?*'\"$]", "", name)
    s = re.sub(r"[/\\.{}_()]", " ", s)
    # Keep only filename-safe characters across Windows/Linux/macOS by
    # removing forbidden path/control symbols and normalizing whitespace.
    s = _INVALID_FILENAME_CHARS_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s)
    s = s.strip()
    # Windows disallows trailing dots/spaces in file/folder names.
    s = s.rstrip(" .")
    if not s:
        return ""
    # Reserved Windows device names are forbidden even with extensions.
    basename = s.split(".", 1)[0].strip().lower()
    if basename in _WINDOWS_RESERVED_BASENAMES:
        s = f"{s} file"
    return s


def _clean_text(text):
    if not isinstance(text, str):
        return ""
    return re.sub(r"[\n\r]+", " ", text).strip()


def _letters_only(text):
    """Keep word chars, strip digits, then take first 30 chars (for get_format_name)."""
    s = re.sub(r"[^\w]+", " ", text)
    s = re.sub(r"\d", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def get_format_name(format_obj):
    if not format_obj:
        return ""
    examples = (
        format_obj.get("examples")
        if isinstance(format_obj, dict)
        else getattr(format_obj, "examples", None)
    )
    if isinstance(examples, list):
        for example in examples:
            text = example if isinstance(example, str) else ""
            name = _letters_only(text)[:50].strip()
            if name:
                return clean_name(name)
    name_attr = (
        format_obj.get("name")
        if isinstance(format_obj, dict)
        else getattr(format_obj, "name", None)
    )
    return clean_name(name_attr or "")


@dataclass
class ValidationError(Exception):
    """Structured validation error with location (file, example) and fix hints.
    Used both as raised exception (parse_format_file, compile_regex) and as value in lists.
    """

    kind: str  # one of: invalid_format, unknown_column, invalid_name,
    # example_no_match, group_count_mismatch, cross_match, regex_error
    file_path: str
    message: str
    example_index: Optional[int] = None
    example_text: Optional[str] = None
    other_file_path: Optional[str] = None
    expected_name: Optional[str] = None

    def __str__(self):
        if self.file_path and not self.message.startswith(self.file_path):
            return f"{self.file_path}: {self.message}"
        return self.message


def compile_regex(regex_line, file_path):
    """Parse /pattern/flags or plain pattern; return compiled regex. Raises ValidationError."""
    pattern = _clean_text(regex_line)
    flags = 0
    if regex_line.startswith("/") and regex_line.rfind("/") > 0:
        last_slash = regex_line.rfind("/")
        pattern = regex_line[1:last_slash]
        flags_str = regex_line[last_slash + 1 :]
        if "i" in flags_str:
            flags |= re.IGNORECASE
        if "u" in flags_str:
            flags |= re.UNICODE
        if "m" in flags_str:
            flags |= re.MULTILINE
        if "s" in flags_str:
            flags |= re.DOTALL
    try:
        return re.compile(pattern, flags)
    except re.error as e:
        raise ValidationError(
            kind="regex_error", file_path=file_path, message=f"Invalid regex: {e}"
        )


def _example_preview(text, max_len=60):
    """First max_len chars of cleaned text, with … if longer."""
    t = _clean_text(text)
    return (t[:max_len] + "…") if len(t) > max_len else t


def validate_format_columns(fmt, file_path=""):
    """Return list of ValidationErrors for disallowed column names."""
    errors: List[ValidationError] = []
    for col in fmt.regex_group_names:
        name = normalize_column_name(col)
        if name not in ALLOWED_COLUMNS:
            errors.append(
                ValidationError(
                    kind="unknown_column",
                    file_path=file_path,
                    message=f"Unknown column: {col}",
                )
            )
    return errors


def validate_format_examples(fmt, file_path="", compiled_regex=None):
    """Return list of ValidationErrors for regex match and group count."""
    errors: List[ValidationError] = []
    if compiled_regex is None:
        try:
            compiled_regex = compile_regex(fmt.regex, file_path)
        except ValidationError as e:
            return [e]
    expected_groups = len(fmt.regex_group_names)
    total = len(fmt.examples)
    for idx, example in enumerate(fmt.examples):
        trimmed = _clean_text(example)
        preview = _example_preview(example)
        ctx = f"example {idx + 1}/{total}: {preview}"
        try:
            match = compiled_regex.search(trimmed)
            if not match:
                errors.append(
                    ValidationError(
                        kind="example_no_match",
                        file_path=file_path,
                        message=f"{ctx}: example does not match its regex",
                        example_index=idx,
                        example_text=example,
                    )
                )
                continue
            group_count = len(match.groups())
            if group_count != expected_groups and expected_groups > 0:
                errors.append(
                    ValidationError(
                        kind="group_count_mismatch",
                        file_path=file_path,
                        message=f"{ctx}: expected {expected_groups} groups, got {group_count}",
                        example_index=idx,
                        example_text=example,
                    )
                )
        except Exception as e:
            errors.append(
                ValidationError(
                    kind="regex_error",
                    file_path=file_path,
                    message=f"{ctx}: {e}",
                    example_index=idx,
                    example_text=example,
                )
            )
    return errors


def validate_format_name(format_name, fmt, file_path=""):
    """Return list of ValidationErrors for format file/dir name."""
    errors: List[ValidationError] = []
    cleaned = clean_name(format_name)
    if format_name != cleaned:
        errors.append(
            ValidationError(
                kind="invalid_name",
                file_path=file_path,
                message="Invalid format file name (expected clean name)",
                expected_name=cleaned,
            )
        )
    expected = get_format_name(fmt)
    if expected and format_name != expected:
        errors.append(
            ValidationError(
                kind="invalid_name",
                file_path=file_path,
                message=f"Invalid format file name, must be {expected}",
                expected_name=expected,
            )
        )
    return errors


def validate_cross_match(formats_with_regex):
    """formats_with_regex: list of (SmsFormat, compiled_regex, file_path).
    Returns list of ValidationErrors for cross-match.
    """
    errors: List[ValidationError] = []
    for idx, (fmt, compiled, file_path) in enumerate(formats_with_regex):
        for ex_idx, example in enumerate(fmt.examples):
            trimmed = _clean_text(example)
            for other_idx, (_, other_compiled, other_path) in enumerate(formats_with_regex):
                if idx == other_idx:
                    continue
                try:
                    if other_compiled.search(trimmed):
                        preview = _example_preview(example)
                        errors.append(
                            ValidationError(
                                kind="cross_match",
                                file_path=file_path,
                                message=(
                                    f"example {ex_idx + 1}/{len(fmt.examples)}: "
                                    f"{preview} — matches {other_path}"
                                ),
                                example_index=ex_idx,
                                example_text=example,
                                other_file_path=other_path,
                            )
                        )
                        break
                except Exception as e:
                    errors.append(
                        ValidationError(
                            kind="regex_error",
                            file_path=other_path,
                            message=f"Regex error for example: {e}",
                            example_index=ex_idx,
                            example_text=example,
                        )
                    )
    return errors


def validate_sms_format_for_import(fmt):
    """Return list of errors for format entry from diff (company_id, id, derivable name)."""
    errors = []
    if fmt.company_id is None or not fmt.id:
        errors.append("Format entry missing companyId or id")
    name = get_format_name(fmt)
    if not name:
        errors.append(f"Format entry missing example {fmt.id}")
    return errors


def validate_sms_format(fmt, file_path="", format_name=None, compiled_regex=None):
    """Run column, example, and optional name validation; return list of ValidationErrors."""
    errors: List[ValidationError] = []
    errors.extend(validate_format_columns(fmt, file_path))
    errors.extend(validate_format_examples(fmt, file_path, compiled_regex=compiled_regex))
    if format_name is not None:
        errors.extend(validate_format_name(format_name, fmt, file_path))
    return errors
