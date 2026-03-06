import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any, Awaitable, Callable, Dict, List, Optional

from openai import AsyncOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diff import commit_file
from sms_format import (
    ALLOWED_COLUMNS,
    AMOUNT_COLUMNS,
    SmsFormat,
    ValidationError,
    _clean_text,
    compile_regex,
    normalize_column_name,
    validate_cross_match,
)
from sms_format_repository import (
    Company,
    find_company_by_id,
    list_formats_with_files,
    save_format,
)

OPENAI_API_KEY = os.environ.get(
    "OPENAI_API_KEY",
    "",
)
client_openai = AsyncOpenAI(api_key=OPENAI_API_KEY)
DEBUG_LLM_OUTPUT = False


@dataclass
class SmsFormatGenerationResult:
    status: str
    reason: str
    sms_type: Optional[str]
    sms_format: Optional[SmsFormat]


@dataclass
class RegexRetryResult:
    valid_regex: Optional[str]
    last_generated_regex: Optional[str]


async def run_prompt(
    prompt: str,
    system_message: str,
    model="gpt-5-mini",
    output_format="text",
    max_tokens=4096,
    temperature=0,
):
    """
    Use for one-off responses

    Returns:
        str or dict: Generated response as text or JSON.
    """
    if output_format not in {"text", "json"}:
        raise ValueError("Output_format must be either 'text' or 'json'.")

    # Construct the request payload
    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ],
        # "temperature": temperature,
        # "max_tokens": max_tokens,
    }
    if model == "gpt-5-mini":
        request_payload["reasoning_effort"] = "low"
        request_payload["verbosity"] = "low"

    if model == "gpt-5":
        request_payload["reasoning_effort"] = "low"
        request_payload["verbosity"] = "low"

    # Add response format for JSON output
    if output_format == "json":
        request_payload["response_format"] = {"type": "json_object"}

    try:
        completion = await client_openai.chat.completions.create(**request_payload)
        result = completion.choices[0].message.content.strip()

        if output_format == "json":
            result = json.loads(result)

        if DEBUG_LLM_OUTPUT:
            print("LLM output:", result)

        return result
    except Exception as e:
        raise RuntimeError(f"Error during OpenAI API call: {e}")


DEFAULT_ENTITY_EXTRACTION_MODEL = "gpt-5-mini"
DEFAULT_REGEX_GENERATION_MODEL = "gpt-5-mini"
DEFAULT_REGEX_VALIDATION_MODEL = "gpt-5-mini"
DEFAULT_SMS_CLASSIFICATION_MODEL = "gpt-4.1"
ENTITY_GUIDE_PATH = Path("docs/transaction_sms_entities_extraction_guide.md")
REGEX_GUIDE_PATH = Path("docs/transaction_sms_regex_writing_guide.md")
_DOC_CACHE: Dict[Path, str] = {}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_doc_text(doc_relative_path: Any) -> str:
    """
    Load project doc text with in-memory cache.

    Reusable for any future docs used in prompts.
    """
    relative_path = Path(doc_relative_path)
    cache_key = relative_path
    if cache_key in _DOC_CACHE:
        return _DOC_CACHE[cache_key]

    absolute_path = _project_root() / relative_path
    with absolute_path.open("r", encoding="utf-8") as doc_file:
        content = doc_file.read()
    _DOC_CACHE[cache_key] = content
    return content


def _is_valid_entity_name(entity: str) -> bool:
    base_name = normalize_column_name(entity)
    return base_name in ALLOWED_COLUMNS


def _normalize_entity_names(raw_entities: Any) -> List[str]:
    if not isinstance(raw_entities, list):
        raise ValueError("LLM response must contain 'entities' as a list.")

    normalized: List[str] = []
    for idx, raw_entity in enumerate(raw_entities):
        if not isinstance(raw_entity, str):
            raise ValueError(
                f"LLM entity at index {idx} must be a string, got: {type(raw_entity).__name__}."
            )
        entity = raw_entity.strip()
        if not entity:
            raise ValueError(f"LLM entity at index {idx} is empty.")
        if not _is_valid_entity_name(entity):
            raise ValueError(f"LLM returned invalid entity: '{entity}'.")
        normalized.append(entity)
    return normalized


def _normalize_entity_items(raw_entities: Any) -> List[Dict[str, str]]:
    if not isinstance(raw_entities, list):
        raise ValueError("LLM response must contain 'entities' as a list.")

    normalized_items: List[Dict[str, str]] = []
    for idx, raw_item in enumerate(raw_entities):
        if not isinstance(raw_item, dict):
            raise ValueError(
                f"LLM entity item at index {idx} must be an object, got: {type(raw_item).__name__}."
            )

        raw_name = raw_item.get("name")
        raw_value = raw_item.get("value")

        if not isinstance(raw_name, str):
            raise ValueError(f"LLM entity item at index {idx} has invalid 'name' field.")
        if not isinstance(raw_value, str):
            raise ValueError(f"LLM entity item at index {idx} has invalid 'value' field.")

        name = raw_name.strip()
        value = raw_value.strip()

        if not name:
            raise ValueError(f"LLM entity item at index {idx} has empty 'name'.")
        if not value:
            raise ValueError(f"LLM entity item at index {idx} has empty 'value'.")
        if not _is_valid_entity_name(name):
            raise ValueError(f"LLM returned invalid entity name: '{name}'.")

        normalized_items.append({"name": name, "value": value})

    return normalized_items


def _normalize_explanation(raw_explanation: Any) -> str:
    if isinstance(raw_explanation, str):
        return raw_explanation.strip()
    return ""


def _normalize_sms_type(raw_sms_type: Any) -> str:
    if not isinstance(raw_sms_type, str):
        return "undefined"
    normalized = raw_sms_type.strip().lower()
    allowed_types = {
        "ad",
        "failed_transaction",
        "otp",
        "transaction",
        "undefined",
    }
    return normalized if normalized in allowed_types else "undefined"


def _normalize_plain_llm_text(raw_text: Any) -> str:
    if not isinstance(raw_text, str):
        raise ValueError("LLM response must be a plain text string.")
    return raw_text.strip()


def _validation_error_to_regex_generation_text(error: ValidationError) -> str:
    """Convert ValidationError to LLM-friendly feedback for regex regeneration."""
    if not isinstance(error, ValidationError):
        raise TypeError(f"Expected ValidationError, got {type(error).__name__}")

    example_text = _clean_text(error.example_text or "")

    if error.kind == "cross_match":
        if example_text:
            return (
                "cross_match: current regex matches a foreign example and must be narrowed. "
                f"It MUST NOT match this text: {example_text}"
            )
        return (
            "cross_match: current regex matches a foreign example and must be narrowed "
            "to avoid cross-format collisions."
        )

    if error.kind == "example_no_match":
        if example_text:
            return (
                "example_no_match: current regex does not match target SMS example. "
                f"It MUST match this example: {example_text}"
            )
        return "example_no_match: current regex does not match target SMS example."

    if error.kind == "group_count_mismatch":
        return (
            "group_count_mismatch: captured group count does not match expected "
            "entities order/count. "
            f"Details: {error.message}"
        )

    if error.kind == "regex_error":
        return f"regex_error: regex is invalid and must be fixed. Details: {error.message}"

    return ""


def _format_validation_errors_for_regex_generation(errors: List[ValidationError]) -> str:
    """Build multiline feedback text for LLM from ValidationError list."""
    if not errors:
        return "None"

    lines: List[str] = []
    for error in errors:
        line = _validation_error_to_regex_generation_text(error)
        if not line:
            continue
        lines.append(f"{len(lines) + 1}. {line}")

    return "\n".join(lines) if lines else "None"


def _extract_names_from_entity_items(entity_items: List[Dict[str, str]]) -> List[str]:
    return [item["name"] for item in entity_items]


def _load_company_formats_with_compiled_regex(company_id: str) -> List[tuple[SmsFormat, Any, str]]:
    items = list_formats_with_files(company_id)
    compiled_items: List[tuple[SmsFormat, Any, str]] = []
    for fmt, file_path in items:
        try:
            compiled = compile_regex(fmt.regex, file_path)
        except ValidationError:
            continue
        compiled_items.append((fmt, compiled, file_path))
    return compiled_items


def _matches_existing_company_format(
    sms_text: str,
    company_formats_with_regex: List[tuple[SmsFormat, Any, str]],
) -> bool:
    normalized_sms_text = _clean_text(sms_text)
    for _fmt, compiled, _file_path in company_formats_with_regex:
        try:
            if compiled.search(normalized_sms_text):
                return True
        except Exception:
            continue
    return False


def _make_company_cross_match_validator(
    company_formats_with_regex: List[tuple[SmsFormat, Any, str]],
    candidate_group_names: Optional[List[str]] = None,
) -> Callable[[str, str], List[ValidationError]]:
    candidate_path = "__candidate_generated__"
    normalized_group_names = candidate_group_names or []

    def validator(regex: str, sms_text: str) -> List[ValidationError]:
        try:
            candidate_compiled = compile_regex(regex, candidate_path)
        except ValidationError as exc:
            return [exc]

        candidate_fmt = SmsFormat(
            regex=regex,
            regex_group_names=normalized_group_names,
            examples=[_clean_text(sms_text)],
            name="candidate_generated",
            id="candidate",
        )
        all_formats = list(company_formats_with_regex) + [
            (candidate_fmt, candidate_compiled, candidate_path)
        ]
        cross_errors = validate_cross_match(all_formats)
        return [
            err
            for err in cross_errors
            if isinstance(err, ValidationError)
            and err.kind == "cross_match"
            and err.file_path == candidate_path
        ]

    return validator


def _current_changed_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _annotate_sms_with_group_span(sms_text: str, span: tuple[int, int]) -> str:
    start, end = span
    if start < 0 or end < 0 or end < start or end > len(sms_text):
        return ""
    if start == end:
        return ""
    return f"{sms_text[:start]}[{sms_text[start:end]}]{sms_text[end:]}"


def _validate_regex_runtime(
    regex: str,
    sms_text: str,
    entity_items: Optional[List[Dict[str, str]]] = None,
) -> List[ValidationError]:
    """
    Unified runtime validator:
    - always checks compile + match
    - when entity_items is provided, also checks group count and group values
    """
    errors: List[ValidationError] = []

    try:
        pattern = compile_regex(regex, "")
    except ValidationError as exc:
        exc.example_text = sms_text
        return [exc]

    match = pattern.search(sms_text)
    if not match:
        return [
            ValidationError(
                kind="example_no_match",
                file_path="",
                message="Generated regex does not match the source SMS text.",
                example_text=sms_text,
            )
        ]

    if entity_items is None:
        return []

    groups = match.groups()
    expected_count = len(entity_items)
    if len(groups) != expected_count:
        return [
            ValidationError(
                kind="group_count_mismatch",
                file_path="",
                message=(
                    f"Captured groups count mismatch: expected {expected_count}, got {len(groups)}."
                ),
                example_text=sms_text,
            )
        ]

    for idx, item in enumerate(entity_items):
        name = item["name"].strip()
        expected = item["value"].strip()
        actual_raw = groups[idx]
        actual = str(actual_raw).strip() if actual_raw is not None else ""

        if name in AMOUNT_COLUMNS:
            expected = re.sub(r"\s+", "", expected)
            actual = re.sub(r"\s+", "", actual)

        if actual != expected:
            try:
                span = match.span(idx + 1)
            except IndexError:
                span = (-1, -1)
            annotated_sms_text = _annotate_sms_with_group_span(sms_text, span)
            errors.append(
                ValidationError(
                    kind="regex_error",
                    file_path="",
                    message=(
                        f"Entity mismatch at group #{idx + 1} ({name}): "
                        f"expected '{expected}', got '{actual}'. "
                        f"Annotated SMS text (full SMS with current captured group marked by [ ]): "
                        f"'{annotated_sms_text}'."
                    ),
                    example_text=sms_text,
                )
            )

    return errors


async def _generate_regex_with_retry(
    sms_text: str,
    explanation: str,
    max_attempts: int,
    regex_validation_model: str,
    generate_fn: Callable[
        [Optional[str], Optional[str]],
        Awaitable[Optional[str]],
    ],
    runtime_validators: List[Callable[[str, str], List[ValidationError]]],
) -> RegexRetryResult:
    """
    Shared retry loop for regex generation with pluggable runtime validators.
    """
    previous_regex: Optional[str] = None
    previous_errors: List[ValidationError] = []
    last_generated_regex: Optional[str] = None

    for attempt in range(max_attempts):
        serialized_errors = (
            _format_validation_errors_for_regex_generation(previous_errors)
            if previous_errors
            else None
        )

        if DEBUG_LLM_OUTPUT:
            print(f"Generation attempt: {attempt}, previous errors: {serialized_errors}")

        try:
            regex = await generate_fn(previous_regex, serialized_errors)
            if not isinstance(regex, str) or not regex.strip():
                raise ValueError("Regex generation returned empty result.")
            regex = regex.strip()
            last_generated_regex = regex
        except Exception as exc:
            previous_regex = None
            previous_errors = [
                ValidationError(
                    kind="regex_error",
                    file_path="",
                    message=f"Regex generation failed on attempt {attempt + 1}: {exc}",
                    example_text=sms_text,
                )
            ]
            continue

        runtime_errors: List[ValidationError] = []
        for validator in runtime_validators:
            current_errors = validator(regex, sms_text)
            if current_errors:
                runtime_errors.extend(current_errors)

        if not runtime_errors:
            hardcode_error = await validate_regex_flexibility_with_llm(
                regex=regex,
                sms_text=sms_text,
                explanation=explanation,
                model=regex_validation_model,
            )
            if hardcode_error is not None:
                runtime_errors.append(hardcode_error)

        if not runtime_errors:
            return RegexRetryResult(
                valid_regex=regex,
                last_generated_regex=last_generated_regex,
            )

        previous_regex = regex
        previous_errors = runtime_errors

    return RegexRetryResult(
        valid_regex=None,
        last_generated_regex=last_generated_regex,
    )


async def classify_sms_with_llm(
    sms_text: str, model: str = DEFAULT_SMS_CLASSIFICATION_MODEL
) -> str:
    """
    Classify SMS into:
    ad | failed_transaction | otp | transaction | undefined
    """
    if not isinstance(sms_text, str) or not sms_text.strip():
        raise ValueError("sms_text must be a non-empty string.")

    classification_system_prompt_en = """
You are an AI assistant in a personal finance application.

Your main task is to classify a bank SMS into one of the following categories:
1. 'ad' - advertising SMS clearly containing promotional information
2. 'failed_transaction' - message about a failed operation
   (it may additionally contain details of that failed operation)
   Example: "Neudacnaya operaciya Karta: *4538 Summa: 27.25 AZN Balans: 2483.62 AZN Wolt".
3. 'otp' - SMS with a confirmation code for operation confirmation
   (it may additionally contain details of the operation),
   usually mentioning words like OTP, code, confirmation code,
   or clearly indicating a code request.
4. 'transaction' - regular SMS with bank transaction data
5. 'undefined' - you cannot confidently assign the SMS to other categories.

Bank transaction information that may appear in SMS includes:
1. account balance or available amount (often marked as Balance / Available)
2. account currency (after Balance / Available)
3. account/card number, often **1234 or 1234**5678
4. operation amount
5. operation comment
6. fee
7. transaction currency near operation amount (usually after, but sometimes before amount)
8. merchant/store
9. operation date

It is possible that SMS contains one, several, or no transaction elements.

Return JSON:
{
    "sms_type": "ad" | "failed_transaction" | "otp" | "transaction" | "undefined"
}
"""

    result = await run_prompt(
        system_message=classification_system_prompt_en,
        prompt=f"SMS: {sms_text}",
        model=model,
        output_format="json",
    )
    if not isinstance(result, dict):
        return "undefined"
    return _normalize_sms_type(result.get("sms_type"))


async def generate_failed_transaction_regex(
    sms_text: str,
    previous_regex: Optional[str] = None,
    validation_errors: Optional[str] = None,
    model: str = DEFAULT_REGEX_GENERATION_MODEL,
) -> Optional[str]:
    """
    Generate regex for failed transaction SMS.

    The function focuses only on the failure-indicator phrase and intentionally
    ignores transaction details, mirroring legacy behavior from sms_parser.
    """
    if not isinstance(sms_text, str) or not sms_text.strip():
        raise ValueError("sms_text must be a non-empty string.")
    if previous_regex is not None and (
        not isinstance(previous_regex, str) or not previous_regex.strip()
    ):
        raise ValueError("previous_regex must be None or a non-empty string.")
    if validation_errors is not None and not isinstance(validation_errors, str):
        raise ValueError("validation_errors must be None or a string.")

    failed_transaction_generation_system_prompt_en = dedent(  # noqa: E501
        """
        You are an experienced engineer with strong expertise in regular expressions
        for bank SMS processing.

        You are given a bank SMS telling that an operation has failed.
        You are NOT interested in operation details.

        Your task:
        1) Identify the key phrase/words indicating operation failure.
        2) Write a regex that matches similar SMS based on this key phrase/words.
        3) Return only the regex text.

        Example:
        SMS: "Neudacnaya operaciya Karta: *4538 Summa: 27.25 AZN Balans: 2483.62 AZN Wolt 2025-04-07"
        Key phrase: "Neudacnaya operaciya"
        Regexp: "^Neudacnaya operaciya.*"

        Output format requirements:
        - Return plain text only (regex string only).
        - Do not return JSON.
        - Do not include explanation.
        - Do not wrap output in markdown/code fences.
        """
    )
    has_prior_feedback = (
        isinstance(previous_regex, str)
        and previous_regex.strip()
        and isinstance(validation_errors, str)
        and validation_errors.strip()
    )

    prompt = f"SMS: {sms_text}"
    if has_prior_feedback:
        prompt += dedent(
            f"""

            Previous attempt feedback (MUST be addressed in this generation):
            previous_regex: {previous_regex}
            validation_errors:
            {validation_errors}
            """
        )

    result = await run_prompt(
        system_message=failed_transaction_generation_system_prompt_en,
        prompt=prompt,
        model=model,
        output_format="text",
    )
    regex = _clean_text(_normalize_plain_llm_text(result))
    if not regex or regex.lower() == "none":
        return None
    return regex


async def generate_otp_regex(
    sms_text: str,
    previous_regex: Optional[str] = None,
    validation_errors: Optional[str] = None,
    model: str = DEFAULT_REGEX_GENERATION_MODEL,
) -> Optional[str]:
    """
    Generate regex for OTP/code SMS.

    The function focuses only on the OTP-indicator phrase and intentionally
    ignores operation details, mirroring legacy behavior from sms_parser.
    """
    if not isinstance(sms_text, str) or not sms_text.strip():
        raise ValueError("sms_text must be a non-empty string.")
    if previous_regex is not None and (
        not isinstance(previous_regex, str) or not previous_regex.strip()
    ):
        raise ValueError("previous_regex must be None or a non-empty string.")
    if validation_errors is not None and not isinstance(validation_errors, str):
        raise ValueError("validation_errors must be None or a string.")

    otp_generation_system_prompt_en = dedent(  # noqa: E501
        """
        You are an experienced engineer with strong expertise in regular expressions
        for bank SMS processing.

        You are given a bank SMS with an operation confirmation code (OTP).
        These are SMS messages that mention OTP, code, confirmation code, or
        clearly indicate a request for an operation confirmation code.
        You are NOT interested in operation details.

        Your task:
        1) Identify the key phrase/words indicating this is an OTP/code SMS.
        2) Write a regex that matches similar SMS based on this key phrase/words.
        3) Return only the regex text.

        Example:
        SMS: "Your OTP is 832940, valid in 1 minutes. Transaction details: QR Pay, amount 460,000 VND on Mobile of VCB Digibank"
        Regexp: "^Your OTP.*"

        Output format requirements:
        - Return plain text only (regex string only).
        - Do not return JSON.
        - Do not include explanation.
        - Do not wrap output in markdown/code fences.
        """
    )
    has_prior_feedback = (
        isinstance(previous_regex, str)
        and previous_regex.strip()
        and isinstance(validation_errors, str)
        and validation_errors.strip()
    )

    prompt = f"SMS: {sms_text}"
    if has_prior_feedback:
        prompt += dedent(
            f"""

            Previous attempt feedback (MUST be addressed in this generation):
            previous_regex: {previous_regex}
            validation_errors:
            {validation_errors}
            """
        )

    result = await run_prompt(
        system_message=otp_generation_system_prompt_en,
        prompt=prompt,
        model=model,
        output_format="text",
    )
    regex = _normalize_plain_llm_text(result)
    if not regex or regex.strip().lower() == "none":
        return None
    return regex.strip()


async def extract_transaction_entities_from_sms(
    sms_text: str, model: str = DEFAULT_ENTITY_EXTRACTION_MODEL
) -> Dict[str, Any]:
    """
    Extract transaction entities from one SMS text using LLM and guide rules.

    Returns JSON-like dict:
    {
        "entities": [{"name": "...", "value": "..."}],
        "explanation": "..."
    }
    """
    if not isinstance(sms_text, str) or not sms_text.strip():
        raise ValueError("sms_text must be a non-empty string.")

    guide_text = load_doc_text(ENTITY_GUIDE_PATH)
    system_message = dedent(
        f"""
        You are an expert bank SMS transaction-entity extractor.
        Follow the guide exactly and extract only entities listed there.
        Do not generate regex.

        Guide:
        {guide_text}
        """
    )

    prompt = dedent(
        f"""
        Extract transaction entities from the SMS below.
        Return JSON only with fields:
        {{
          "entities": [{{"name": "entity_name_in_order", "value": "exact_extracted_value"}}],
          "explanation": "reasoning text preserving entity order"
        }}

        SMS:
        {sms_text}
        """
    )

    llm_result = await run_prompt(
        prompt=prompt,
        system_message=system_message,
        model=model,
        output_format="json",
    )

    if not isinstance(llm_result, dict):
        llm_result = {}

    entities = _normalize_entity_items(llm_result.get("entities"))
    explanation = _normalize_explanation(llm_result.get("explanation"))

    return {
        "entities": entities,
        "explanation": explanation,
    }


async def generate_transaction_regex_from_context(
    sms_text: str,
    entities: List[str],
    explanation: str,
    previous_regex: Optional[str] = None,
    validation_errors: Optional[str] = None,
    model: str = DEFAULT_REGEX_GENERATION_MODEL,
) -> str:
    """
    Generate transaction regex from sms/entities/explanation and previous validation errors.

    Returns:
        str: regex only.
    """
    if not isinstance(sms_text, str) or not sms_text.strip():
        raise ValueError("sms_text must be a non-empty string.")
    if not isinstance(explanation, str) or not explanation.strip():
        raise ValueError("explanation must be a non-empty string.")
    if previous_regex is not None and (
        not isinstance(previous_regex, str) or not previous_regex.strip()
    ):
        raise ValueError("previous_regex must be None or a non-empty string.")
    if validation_errors is not None and not isinstance(validation_errors, str):
        raise ValueError("validation_errors must be None or a string.")

    normalized_entities = _normalize_entity_names(entities)
    previous_regex = previous_regex.strip() if isinstance(previous_regex, str) else None
    has_prior_feedback = (
        previous_regex is not None
        and isinstance(validation_errors, str)
        and validation_errors.strip() != ""
    )
    regex_guide_text = load_doc_text(REGEX_GUIDE_PATH)

    system_message = dedent(
        f"""
        You are an expert bank SMS transaction regex writer.
        Follow the guide exactly.
        Input entities/explanation are the source of truth.
        Return plain text only (regex string only, no JSON, no markdown).

        Guide:
        {regex_guide_text}
        """
    )

    prompt = dedent(
        f"""
        Generate a regex for this transaction SMS.
        Return only regex text as a plain string.
        Do not return JSON.
        Do not wrap output in markdown/code fences.

        Input:
        sms_text: {sms_text}
        entities: {normalized_entities}
        explanation: {explanation}
        """
    )
    if has_prior_feedback:
        prompt += dedent(
            f"""

            Previous attempt feedback (MUST be addressed in this generation):
            previous_regex: {previous_regex}
            validation_errors:
            {validation_errors}
            """
        )

    llm_result = await run_prompt(
        prompt=prompt,
        system_message=system_message,
        model=model,
        output_format="text",
    )
    regex = _normalize_plain_llm_text(llm_result)
    if not regex.strip():
        raise ValueError("LLM response must contain non-empty regex text.")

    return regex.strip()


async def generate_sms_format(
    sms_text: str,
    max_attempts: int = 5,
    company_id: Optional[str] = None,
    classification_model: str = DEFAULT_SMS_CLASSIFICATION_MODEL,
    entity_extraction_model: str = DEFAULT_ENTITY_EXTRACTION_MODEL,
    regex_generation_model: str = DEFAULT_REGEX_GENERATION_MODEL,
    regex_validation_model: str = DEFAULT_REGEX_VALIDATION_MODEL,
    allow_draft: bool = False,
) -> SmsFormatGenerationResult:
    if not isinstance(sms_text, str) or not sms_text.strip():
        raise ValueError("sms_text must be a non-empty string.")
    if not isinstance(max_attempts, int) or max_attempts <= 0:
        raise ValueError("max_attempts must be a positive integer.")

    resolved_company: Optional[Company] = None
    if company_id is not None:
        resolved_company = find_company_by_id(str(company_id).strip())
        if resolved_company is None:
            raise ValueError(f"Company not found for id '{company_id}'.")
    company_formats_with_regex: List[tuple[SmsFormat, Any, str]] = []
    if resolved_company is not None:
        if resolved_company.id is None:
            raise ValueError(f"Company '{resolved_company.name}' does not have a company id.")
        resolved_company_id = str(resolved_company.id)
        company_formats_with_regex = _load_company_formats_with_compiled_regex(resolved_company_id)

        # Required pre-check: if SMS already matches existing format, skip generation entirely.
        if _matches_existing_company_format(sms_text, company_formats_with_regex):
            return SmsFormatGenerationResult(
                sms_format=None,
                status="duplicate",
                reason="matches_existing",
                sms_type=None,
            )

    sms_type = await classify_sms_with_llm(
        sms_text=sms_text,
        model=classification_model,
    )
    if sms_type == "ad":
        return SmsFormatGenerationResult(
            sms_format=None,
            status="ad",
            reason="ad",
            sms_type=sms_type,
        )

    if sms_type == "transaction":
        extraction_result = await extract_transaction_entities_from_sms(
            sms_text=sms_text,
            model=entity_extraction_model,
        )
        entity_items = extraction_result["entities"]
        explanation = extraction_result["explanation"]
        if not entity_items:
            return SmsFormatGenerationResult(
                sms_format=None,
                status="failed",
                reason="no_entities",
                sms_type=sms_type,
            )

        entity_names = _extract_names_from_entity_items(entity_items)

        async def transaction_generate_fn(
            previous_regex: Optional[str], serialized_errors: Optional[str]
        ) -> Optional[str]:
            return await generate_transaction_regex_from_context(
                sms_text=sms_text,
                entities=entity_names,
                explanation=explanation,
                previous_regex=previous_regex,
                validation_errors=serialized_errors,
                model=regex_generation_model,
            )

        transaction_runtime_validators = [
            lambda regex, text: _validate_regex_runtime(
                regex=regex,
                sms_text=text,
                entity_items=entity_items,
            )
        ]
        if resolved_company is not None:
            transaction_runtime_validators.append(
                _make_company_cross_match_validator(
                    company_formats_with_regex=company_formats_with_regex,
                    candidate_group_names=entity_names,
                )
            )

        retry_result = await _generate_regex_with_retry(
            sms_text=sms_text,
            explanation=explanation,
            max_attempts=max_attempts,
            regex_validation_model=regex_validation_model,
            generate_fn=transaction_generate_fn,
            runtime_validators=transaction_runtime_validators,
        )
        regex = retry_result.valid_regex
        if isinstance(regex, str) and regex.strip():
            return SmsFormatGenerationResult(
                sms_format=SmsFormat(
                    regex=regex,
                    regex_group_names=entity_names,
                    examples=[sms_text],
                    company_id=str(resolved_company.id)
                    if resolved_company and resolved_company.id
                    else None,
                ),
                status="transaction",
                reason="generated",
                sms_type=sms_type,
            )
        if (
            allow_draft
            and isinstance(retry_result.last_generated_regex, str)
            and retry_result.last_generated_regex.strip()
        ):
            return SmsFormatGenerationResult(
                sms_format=SmsFormat(
                    regex=retry_result.last_generated_regex,
                    regex_group_names=entity_names,
                    examples=[sms_text],
                    company_id=str(resolved_company.id)
                    if resolved_company and resolved_company.id
                    else None,
                ),
                status="transaction_draft",
                reason="draft_generated",
                sms_type=sms_type,
            )
        return SmsFormatGenerationResult(
            sms_format=None,
            status="failed",
            reason="regex_not_generated",
            sms_type=sms_type,
        )

    if sms_type in {"otp", "failed_transaction"}:
        explanation = (
            "SMS type is otp. Validate only hardcoded/overly-narrow patterns "
            "for OTP-style classifier regex."
            if sms_type == "otp"
            else "SMS type is failed_transaction. Validate only hardcoded/overly-narrow "
            "patterns for failed-operation classifier regex."
        )

        async def non_transaction_generate_fn(
            previous_regex: Optional[str], serialized_errors: Optional[str]
        ) -> Optional[str]:
            if sms_type == "otp":
                return await generate_otp_regex(
                    sms_text=sms_text,
                    previous_regex=previous_regex,
                    validation_errors=serialized_errors,
                    model=regex_generation_model,
                )
            return await generate_failed_transaction_regex(
                sms_text=sms_text,
                previous_regex=previous_regex,
                validation_errors=serialized_errors,
                model=regex_generation_model,
            )

        non_transaction_runtime_validators = [
            lambda regex, text: _validate_regex_runtime(
                regex=regex,
                sms_text=text,
                entity_items=None,
            )
        ]
        if resolved_company is not None:
            non_transaction_runtime_validators.append(
                _make_company_cross_match_validator(
                    company_formats_with_regex=company_formats_with_regex,
                    candidate_group_names=[],
                )
            )

        retry_result = await _generate_regex_with_retry(
            sms_text=sms_text,
            explanation=explanation,
            max_attempts=max_attempts,
            regex_validation_model=regex_validation_model,
            generate_fn=non_transaction_generate_fn,
            runtime_validators=non_transaction_runtime_validators,
        )
        regex = retry_result.valid_regex
        if isinstance(regex, str) and regex.strip():
            return SmsFormatGenerationResult(
                sms_format=SmsFormat(
                    regex=regex,
                    regex_group_names=[],
                    examples=[sms_text],
                    company_id=str(resolved_company.id)
                    if resolved_company and resolved_company.id
                    else None,
                ),
                status=sms_type,
                reason="generated",
                sms_type=sms_type,
            )
        if (
            allow_draft
            and isinstance(retry_result.last_generated_regex, str)
            and retry_result.last_generated_regex.strip()
        ):
            return SmsFormatGenerationResult(
                sms_format=SmsFormat(
                    regex=retry_result.last_generated_regex,
                    regex_group_names=[],
                    examples=[sms_text],
                    company_id=str(resolved_company.id)
                    if resolved_company and resolved_company.id
                    else None,
                ),
                status=f"{sms_type}_draft",
                reason="draft_generated",
                sms_type=sms_type,
            )
        return SmsFormatGenerationResult(
            sms_format=None,
            status="failed",
            reason="regex_not_generated",
            sms_type=sms_type,
        )

    return SmsFormatGenerationResult(
        sms_format=None,
        status="failed",
        reason="unexpected_sms_type",
        sms_type=sms_type,
    )


async def validate_regex_flexibility_with_llm(
    regex: str, sms_text: str, explanation: str, model: str = DEFAULT_REGEX_VALIDATION_MODEL
) -> Optional[ValidationError]:
    """
    Validate regex for hardcoded/overly-rigid patterns using LLM.

    Input:
        regex: regex text to validate
        sms_text: source SMS text
        explanation: extraction explanation for current SMS

    Returns:
        Optional[ValidationError]: ValidationError with all detected problems
            or None if no problems are found.
    """
    if regex is None or (isinstance(regex, str) and not regex.strip()):
        return None
    if not isinstance(regex, str):
        raise ValueError("regex must be a string.")
    if not isinstance(sms_text, str) or not sms_text.strip():
        raise ValueError("sms_text must be a non-empty string.")
    if not isinstance(explanation, str) or not explanation.strip():
        raise ValueError("explanation must be a non-empty string.")

    regex_flexibility_validation_system_prompt_en = dedent(  # noqa: E501
        r"""
        You are an expert validator of regular expressions for bank SMS.

        Your task is to validate regexp for two things only:
        1) hardcoded literal values,
        2) clearly overly-narrow constraints that will reject valid variations
           implied by this SMS template (especially text fields like merchant/payee/comment).

        Do NOT provide generic stylistic advice. Report only concrete validation problems.

        CRITICAL ERRORS to detect:
        1. Hardcoded currencies: (RUB), (USD), (₽), etc.
        2. Hardcoded merchant names: (PYATEROCHK), (MAGIYA SVETA), (OZON), etc.
        3. Hardcoded amounts: (1468,88), (1782), etc.
        4. Hardcoded card/account numbers: (8587), (1420), etc.

        EXAMPLES OF INCORRECT EXPRESSIONS:
        Example 1:
        Incorrect - hardcoded (RUB) and (PYATEROCHK)
        sms_text: Покупка, карта *8587. 1468,88 RUB. PYATEROCHK. Доступно 32095,98 RUB
        regexp: ^Покупка,\s+карта\s+\*(8587)\.\s+(1468,88)\s+(RUB)\.\s+(PYATEROCHK)\.\s+Доступно\s+(32095,98)\s+(RUB)$

        Example 2:
        Incorrect - hardcoded (MAGIYA SVETA)
        sms_text: Покупка, карта *8587. 1782 RUB. MAGIYA SVETA. Доступно 29695,98 RUB
        regexp: Покупка,\s+карта\s+\*(\d{4})\.\s+([0-9]{4})\s+([A-Z]{3})\.\s+(MAGIYA SVETA)\.\s+Доступно\s+(-?[0-9][\d\s]*,[0-9]{2})\s+([A-Z]{3})

        CORRECT approach is to use generalized patterns WITHOUT introducing new literals:
        - Instead of concrete currency literals, use a generic token pattern based on observed token shape:
          use [A-Z]{3} only when currency is ISO-like in this SMS template; otherwise, prefer (.+?)
        - Instead of concrete merchant literals, use a generic text capture (for example: (.+?) with a delimiter
          that is explicitly present in this SMS template).
        - Instead of concrete amount literals, use a numeric pattern (for example: (\d[\d\s.,]*))
        - Instead of concrete account number literals, use a digit pattern (for example: (\d{4}))

        STRICT RULES FOR FIX SUGGESTIONS:
        - Do NOT suggest any concrete currency literals/symbols/codes in fixes (no RUB/USD/₽/р/etc.).
        - Do NOT suggest mixed literal alternatives like (?:[A-Z]{3}|[₽р]) because it introduces hardcoded literals.
        - Do NOT invent new lexical values absent from the current SMS.
        - Do NOT invent delimiters that are not present in the SMS template. Use only observed delimiters/anchors.
        - Keep suggestions generic and based on observed token shape only.

        CLEARLY OVERLY-NARROW CONSTRAINTS to detect:
        - Character classes for text fields that are obviously too narrow for realistic merchant/comment values
          (for example `[A-Z0-9 ]+` when merchant may contain lowercase letters, punctuation, symbols, or non-Latin chars).
        - Constraints that conflict with observed token behavior in SMS/explanation and can block valid similar SMS.

        Analyze provided expression and determine ONLY:
        1. Whether it contains hardcoded values from the 4 categories above
        2. Whether it has clearly overly-narrow constraints as defined above
        3. What exact problems are found

        Return plain text only:
        - If problems are found: return a detailed description of all found issues and how to fix them.
        - If no problems are found: return exactly "none".
        - If there are no hardcoded/clearly-over-narrow issues, return exactly "none".
        Do not return JSON.
        Do not wrap output in markdown/code fences.
        """
    )

    validation_prompt = dedent(
        f"""
        Validate this regexp for:
        1) hardcoded literals from the four categories
           (currency, merchant, amount, account/card number),
        2) clearly overly-narrow constraints that would reject valid similar SMS.

        SMS text: {sms_text}
        Explanation: {explanation}
        Regexp: {regex}

        Return plain text only:
        - detailed issues + generic fix suggestions (no literal currencies/codes/symbols), OR
        - "none" if no issues.
        """
    )

    result = await run_prompt(
        system_message=regex_flexibility_validation_system_prompt_en,
        prompt=validation_prompt,
        model=model,
        output_format="text",
    )
    issues_text = _normalize_plain_llm_text(result)

    if not issues_text or issues_text.lower() == "none":
        return None

    return ValidationError(
        kind="regex_error",
        file_path="",
        message=issues_text,
        example_text=sms_text,
    )


def _save_generated_format_with_commit(
    sms_format: SmsFormat,
    company_id: str,
    *,
    is_draft: bool = False,
) -> Optional[str]:
    resolved_company = find_company_by_id(str(company_id).strip())
    if resolved_company is None or resolved_company.id is None:
        raise ValueError("Valid --company is required for --save (company id expected).")

    sms_format.company_id = str(resolved_company.id)
    sms_format.changed = _current_changed_timestamp()

    save_result = save_format(sms_format, sms_format.company_id)
    if not save_result.changed_paths:
        return None

    commit_title = (
        f"[{resolved_company.name}] create format draft"
        if is_draft
        else f"[{resolved_company.name}] create format"
    )
    commit_file(
        save_result.changed_paths,
        commit_title,
        sms_format.changed,
    )
    return commit_title


async def _main_from_stdin() -> int:
    global DEBUG_LLM_OUTPUT
    parser = argparse.ArgumentParser(description="Generate SMS format from stdin SMS text.")
    parser.add_argument("--debug", action="store_true", help="Enable LLM output debug prints.")
    parser.add_argument(
        "--company",
        type=str,
        default=None,
        help="Company id.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save generated format and create git commit.",
    )
    parser.add_argument(
        "--allow-draft",
        action="store_true",
        help="Allow draft fallback using last generated regex.",
    )
    args = parser.parse_args()
    DEBUG_LLM_OUTPUT = bool(args.debug)
    json_output = not args.debug
    save_enabled = True if not args.debug else bool(args.save)

    sms_text = sys.stdin.read().strip()
    if not sms_text:
        print("stdin is empty: provide SMS text via stdin", file=sys.stderr)
        return 1

    if save_enabled and not args.company:
        print("--save requires --company", file=sys.stderr)
        return 1

    generation_result = await generate_sms_format(
        sms_text=sms_text,
        company_id=args.company,
        allow_draft=bool(args.allow_draft),
    )
    sms_format = generation_result.sms_format
    if sms_format is None:
        if json_output:
            print(
                json.dumps(
                    {
                        "status": generation_result.status,
                        "reason": generation_result.reason,
                        "commit_title": None,
                    }
                )
            )
            return 0
        print("none")
        return 0

    commit_title: Optional[str] = None
    if save_enabled:
        try:
            commit_title = _save_generated_format_with_commit(
                sms_format,
                args.company,
                is_draft=generation_result.status.endswith("_draft"),
            )
        except Exception as exc:
            if json_output:
                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "reason": f"save_commit_error: {exc}",
                            "commit_title": None,
                        }
                    )
                )
                return 0
            print(f"Failed to save/commit generated format: {exc}", file=sys.stderr)
            return 1

    if json_output:
        output_status = generation_result.status
        output_reason = generation_result.reason
        if save_enabled and not commit_title:
            output_status = "duplicate"
            output_reason = "no_changes_to_commit"
        print(
            json.dumps(
                {
                    "status": output_status,
                    "reason": output_reason,
                    "commit_title": commit_title,
                }
            )
        )
        return 0

    print(sms_format.regex if isinstance(sms_format.regex, str) and sms_format.regex else "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main_from_stdin()))
