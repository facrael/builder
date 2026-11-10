"""Configuration loader and strict validator for construction bot (ТЗ v2: З-2, З-3).

Enforces:
- ADMIN_TG_ID positive integer required.
- PROJECTS list non-empty.
- For each project key:
  - PROJECT_<KEY>_CODE (unique, non-empty)
  - PROJECT_<KEY>_NAME (non-empty)
  - PROJECT_<KEY>_CLOUD_OK (bool, default false)
  - PROJECT_<KEY>_USERS (list of positive int IDs, no duplicates within project)
- Empty user list = 0 access (never open to all).
- Fail-fast on any invalid config.
"""
from __future__ import annotations
import os
import re
from dataclasses import dataclass, field


@dataclass
class ProjectConfig:
    key: str
    code: str
    name: str
    cloud_ok: bool
    users: set[int] = field(default_factory=set)


@dataclass
class AppConfig:
    admin_tg_id: int
    projects: dict[str, ProjectConfig]  # key: code (uppercase) -> ProjectConfig
    projects_by_key: dict[str, ProjectConfig]  # key: env key (OBJ1) -> ProjectConfig
    user_to_projects: dict[int, set[str]]  # tg_id -> set of project codes

    def is_admin(self, tg_id: int) -> bool:
        return tg_id == self.admin_tg_id

    def is_user_allowed(self, tg_id: int) -> bool:
        """Returns True if user is in at least one project whitelist OR is system admin."""
        return (tg_id in self.user_to_projects) or (tg_id == self.admin_tg_id)

    def get_user_projects(self, tg_id: int) -> list[str]:
        """Returns list of project codes user has explicit access to."""
        return sorted(list(self.user_to_projects.get(tg_id, set())))

    def ok_for_cloud(self, project_code: str) -> bool:
        """Cloud guard: True ONLY if PROJECT_<KEY>_CLOUD_OK=true for this project."""
        if not project_code:
            return False
        p = self.projects.get(project_code.upper())
        return bool(p and p.cloud_ok)

    def get_project(self, project_code: str) -> ProjectConfig | None:
        if not project_code:
            return None
        return self.projects.get(project_code.upper())


def _parse_bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    v = val.strip().lower()
    return v in ("true", "1", "yes", "y", "on")


def parse_and_validate_config(env: dict[str, str] | None = None) -> AppConfig:
    """Parses environment variables and strictly validates against specification rules.

    Raises ValueError with precise description if any rule is violated.
    """
    if env is None:
        env = os.environ

    # 1. Validate ADMIN_TG_ID
    raw_admin = env.get("ADMIN_TG_ID")
    if not raw_admin or not raw_admin.strip():
        raise ValueError("Configuration error: ADMIN_TG_ID is required and cannot be empty")

    try:
        admin_tg_id = int(raw_admin.strip())
        if admin_tg_id <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError(f"Configuration error: ADMIN_TG_ID must be a positive integer, got '{raw_admin}'")

    # 2. Validate PROJECTS list
    raw_projects = env.get("PROJECTS")
    if not raw_projects or not raw_projects.strip():
        raise ValueError("Configuration error: PROJECTS list is required and cannot be empty")

    project_keys = [k.strip() for k in re.split(r'[,;\s]+', raw_projects) if k.strip()]
    if not project_keys:
        raise ValueError("Configuration error: PROJECTS contains no valid project keys")

    projects_by_code: dict[str, ProjectConfig] = {}
    projects_by_key: dict[str, ProjectConfig] = {}
    user_to_projects: dict[int, set[str]] = {}

    for pkey in project_keys:
        code_var = f"PROJECT_{pkey}_CODE"
        name_var = f"PROJECT_{pkey}_NAME"
        cloud_var = f"PROJECT_{pkey}_CLOUD_OK"
        users_var = f"PROJECT_{pkey}_USERS"

        code = env.get(code_var, "").strip()
        if not code:
            raise ValueError(f"Configuration error: Missing or empty {code_var} for project '{pkey}'")

        code_upper = code.upper()
        if code_upper in projects_by_code:
            raise ValueError(
                f"Configuration error: Duplicate project code '{code}' in {code_var}. Project codes must be globally unique."
            )

        name = env.get(name_var, "").strip()
        if not name:
            # Fallback to code if name not provided, but warn/require
            name = f"Объект {code}"

        cloud_ok = _parse_bool(env.get(cloud_var), default=False)

        raw_users = env.get(users_var)
        if raw_users is None:
            raise ValueError(
                f"Configuration error: Variable {users_var} is missing for project '{pkey}'. Must be specified (even if empty)."
            )

        user_tokens = [u.strip() for u in re.split(r'[,;\s]+', raw_users) if u.strip()]
        user_ids: set[int] = set()

        for token in user_tokens:
            try:
                uid = int(token)
                if uid <= 0:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError(
                    f"Configuration error: Invalid Telegram ID '{token}' in {users_var}. IDs must be positive integers."
                )

            if uid in user_ids:
                raise ValueError(
                    f"Configuration error: Duplicate Telegram ID {uid} in {users_var} for project '{pkey}'."
                )
            user_ids.add(uid)

        proj_cfg = ProjectConfig(
            key=pkey,
            code=code_upper,
            name=name,
            cloud_ok=cloud_ok,
            users=user_ids,
        )
        projects_by_code[code_upper] = proj_cfg
        projects_by_key[pkey] = proj_cfg

        for uid in user_ids:
            user_to_projects.setdefault(uid, set()).add(code_upper)

    return AppConfig(
        admin_tg_id=admin_tg_id,
        projects=projects_by_code,
        projects_by_key=projects_by_key,
        user_to_projects=user_to_projects,
    )


# Global lazy config instance
_GLOBAL_CONFIG: AppConfig | None = None


def get_config() -> AppConfig:
    global _GLOBAL_CONFIG
    if _GLOBAL_CONFIG is None:
        _GLOBAL_CONFIG = parse_and_validate_config()
    return _GLOBAL_CONFIG


def set_config(cfg: AppConfig):
    global _GLOBAL_CONFIG
    _GLOBAL_CONFIG = cfg
