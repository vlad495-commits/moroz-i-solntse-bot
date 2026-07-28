"""Public URL helpers for an admin app mounted below a reverse-proxy prefix."""


def admin_url(request, path: str) -> str:
    root_path = (getattr(request, "scope", {}) or {}).get("root_path", "").rstrip("/")
    return f"{root_path}/{path.lstrip('/')}"
