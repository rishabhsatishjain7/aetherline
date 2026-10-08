import re

from pydantic import BaseModel, field_validator

# Deliberately a simple pattern, not a full RFC 5322 parser: the point is to
# reject obvious non-emails with a 422, not to guarantee deliverability.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserCredentials(BaseModel):
    """Payload for POST /auth/register and POST /auth/login.

    The password is intentionally NOT length-validated here: pydantic's 422
    body echoes the rejected input, which would echo the password straight
    back to the caller. Byte-length validation happens in the register
    handler as a plain `detail` string instead.
    """

    email: str
    password: str

    @field_validator("email")
    @classmethod
    def email_must_be_well_formed(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("invalid email format")
        return value
