import json
import logging
import os
import secrets
from typing import Any

import resend
import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, EmailStr, field_validator

MAX_MESSAGE_LENGTH = 300

load_dotenv()

logger = logging.getLogger(__name__)
app = FastAPI(title="Global Mailer API")

REQUIRED_ENV_VARS = ("RESEND_API_KEY", "MAIL_FROM")
API_KEY_PREFIX = "MAILER_KEY_"


def get_from_address() -> str:
    mail_from = os.environ["MAIL_FROM"].strip()
    from_name = os.getenv("MAIL_FROM_NAME", "Global Mailer").strip()
    if "<" in mail_from and ">" in mail_from:
        return mail_from
    return f"{from_name} <{mail_from}>"


def init_resend() -> str:
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    resend.api_key = os.environ["RESEND_API_KEY"].strip()
    return get_from_address()


def load_api_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    for env_name, value in os.environ.items():
        if not env_name.startswith(API_KEY_PREFIX):
            continue
        key = value.strip()
        if not key:
            continue
        service = env_name.removeprefix(API_KEY_PREFIX).lower().replace("_", "-")
        keys[service] = key
    return keys


def get_service_for_api_key(provided_key: str, api_keys: dict[str, str]) -> str | None:
    for service, stored_key in api_keys.items():
        if secrets.compare_digest(provided_key, stored_key):
            return service
    return None


mail_from = init_resend()
api_keys = load_api_keys()

if not api_keys:
    raise RuntimeError(
        f"No API keys configured. Add at least one env var like {API_KEY_PREFIX}MY_APP=your-secret-key"
    )


async def require_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> str:
    service = get_service_for_api_key(x_api_key.strip(), api_keys)
    if not service:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return service


class EmailSchema(BaseModel):
    email: EmailStr
    subject: str
    body: Any

    @field_validator("email", mode="before")
    @classmethod
    def reject_empty_email(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("Email cannot be empty")
        return value.strip() if isinstance(value, str) else value

    @field_validator("subject", mode="before")
    @classmethod
    def reject_empty_subject(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("Subject cannot be empty")
        return stripped

    @field_validator("body", mode="before")
    @classmethod
    def validate_message(cls, value: object) -> str:
        if value is None:
            text = ""
        elif isinstance(value, str):
            text = value
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, separators=(",", ":"))
        else:
            text = str(value)

        if len(text) > MAX_MESSAGE_LENGTH:
            raise ValueError(
                f"Message must be at most {MAX_MESSAGE_LENGTH} characters"
            )
        return text


def send_email_task(
    email_to: str, subject: str, body: str, service: str
) -> None:
    try:
        resend.Emails.send(
            {
                "from": mail_from,
                "to": [email_to],
                "subject": subject,
                "html": body,
            }
        )
        logger.info("[%s] Email accepted by Resend for delivery to %s", service, email_to)
    except Exception:
        logger.exception("[%s] Failed to send email to %s", service, email_to)


@app.get("/health")
async def health():
    return {
        "status": 200,
        "success": True,
        "message": "I dey very galant boss",
        "mail_provider": "resend",
    }


@app.post("/send-email")
async def send_email(
    payload: EmailSchema,
    background_tasks: BackgroundTasks,
    service: str = Depends(require_api_key),
):
    background_tasks.add_task(
        send_email_task,
        payload.email,
        payload.subject,
        payload.body,
        service,
    )
    return {
        "success": True,
        "message": "Email queued for delivery",
        "service": service,
        "to": payload.email,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8000)
