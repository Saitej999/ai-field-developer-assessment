"""Local-only Bearer token identities for Task 2 development.

These demo tokens are public test fixtures, not production credentials. Replace
this registry with a trusted identity provider before using the gateway outside
local assessment testing. Never log an Authorization header or complete token.
"""

from dataclasses import dataclass
from typing import Literal

from fastapi import Request


Role = Literal["admin", "viewer"]
AuthStatus = Literal["authenticated", "missing", "invalid"]

# Deliberately fake, local-only identities. The resolved context never stores a token.
_DEMO_TOKEN_ROLES: dict[str, Role] = {
    "admin-demo-token": "admin",
    "viewer-demo-token": "viewer",
}


@dataclass(frozen=True)
class AuthContext:
    status: AuthStatus
    role: Role | None = None


def authenticate_authorization_header(authorization: str | None) -> AuthContext:
    """Resolve one Authorization value; invalid credentials gain no identity."""
    if authorization is None:
        return AuthContext(status="missing")

    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token or any(char.isspace() for char in token):
        return AuthContext(status="invalid")

    role = _DEMO_TOKEN_ROLES.get(token)
    if role is None:
        return AuthContext(status="invalid")
    return AuthContext(status="authenticated", role=role)


def authenticate_request(request: Request) -> AuthContext:
    """Ignore client role headers and reject ambiguous Authorization headers."""
    authorization_values = request.headers.getlist("authorization")
    if len(authorization_values) > 1:
        return AuthContext(status="invalid")
    return authenticate_authorization_header(
        authorization_values[0] if authorization_values else None
    )
