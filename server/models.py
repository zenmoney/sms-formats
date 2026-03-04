from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class SmsPayload(BaseModel):
    company_name: str = Field(min_length=1)
    bank_name: Optional[str] = None
    sender: str = Field(min_length=1)
    text: str = Field(min_length=1)
    company_id: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _accept_bank_name_fallback(cls, data):
        if not isinstance(data, dict):
            return data
        if "company_name" in data:
            return data
        bank_name = data.get("bank_name")
        if isinstance(bank_name, str):
            payload = dict(data)
            payload["company_name"] = bank_name
            return payload
        return data


class SmsRequest(BaseModel):
    sms: SmsPayload


StatusValue = Literal[
    "ad",
    "unknown_sender",
    "duplicate",
    "otp",
    "otp_draft",
    "transaction",
    "transaction_draft",
    "failed_transaction",
    "failed_transaction_draft",
    "failed",
]


class SmsResponse(BaseModel):
    status: StatusValue


class DiffPayload(BaseModel):
    companies: List[Dict[str, Any]] = Field(default_factory=list)
    senders: List[Dict[str, Any]] = Field(default_factory=list)
    formats: List[Dict[str, Any]] = Field(default_factory=list)


class DiffRequest(BaseModel):
    diff: DiffPayload = Field(default_factory=DiffPayload)
    lastCommitHash: Optional[str] = None
    lastServerTimestamp: Optional[str] = None


class DiffResponse(BaseModel):
    diff: DiffPayload
    commitHash: str
