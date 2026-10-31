"""Tool-level authorization policy for the local Task 2 gateway."""

from task2_security_gateway.auth import AuthContext


def may_call_tool(tool_name: str, auth: AuthContext) -> bool:
    """Only a recognized admin identity may invoke an admin_ tool."""
    if not tool_name.startswith("admin_"):
        return True
    return auth.status == "authenticated" and auth.role == "admin"
