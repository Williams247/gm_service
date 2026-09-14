import logging
import os
import re
import secrets

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType
from pydantic import BaseModel, EmailStr, field_validator

load_dotenv()

logger = logging.getLogger(__name__)
app = FastAPI(title="Gmail Mailer API")

REQUIRED_ENV_VARS = ("MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM")
API_KEY_PREFIX = "MAILER_KEY_"

def get_gmail_config() -> ConnectionConfig:
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    mail_username = os.environ["MAIL_USERNAME"].strip()
    mail_from = os.environ["MAIL_FROM"].strip()
    mail_password = os.environ["MAIL_PASSWORD"].replace(" ", "").strip()

    if mail_from.lower() != mail_username.lower():
        logger.warning(
            "MAIL_FROM (%s) differs from MAIL_USERNAME (%s); Gmail may reject or misdeliver",
            mail_from,
            mail_username,
        )

    return ConnectionConfig(
        MAIL_USERNAME=mail_username,
        MAIL_PASSWORD=mail_password,
        MAIL_FROM=mail_from,
        MAIL_FROM_NAME=os.getenv("MAIL_FROM_NAME", "Gmail Mailer"),
        MAIL_PORT=int(os.getenv("MAIL_PORT", "587")),
        MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp.gmail.com"),
        MAIL_STARTTLS=os.getenv("MAIL_STARTTLS", "true").lower() == "true",
        MAIL_SSL_TLS=os.getenv("MAIL_SSL_TLS", "false").lower() == "true",
        USE_CREDENTIALS=True,
        VALIDATE_CERTS=True,
    )


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

conf = get_gmail_config()
fast_mail = FastMail(conf)
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
    body: str

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
    def validate_message_digits(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not re.fullmatch(r"\d{4,8}", stripped):
            raise ValueError("Message must contain only digits, between 4 and 8 characters")
        return stripped


async def send_email_async(
    email_to: str, subject: str, body: str, service: str
) -> None:
    message = MessageSchema(
        subject=subject,
        recipients=[email_to],
        body=body,
        subtype=MessageType.html,
    )
    try:
        await fast_mail.send_message(message)
        logger.info("[%s] Email accepted by Gmail for delivery to %s", service, email_to)
    except Exception:
        logger.exception("[%s] Failed to send email to %s", service, email_to)

@app.get("/health")
async def health():
    return {
        "status": 200,
        "success": True,
        "message": "I dey active boss"
    }

@app.post("/send-email")
async def send_email(
    payload: EmailSchema,
    background_tasks: BackgroundTasks,
    service: str = Depends(require_api_key),
):
    background_tasks.add_task(
        send_email_async,
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
