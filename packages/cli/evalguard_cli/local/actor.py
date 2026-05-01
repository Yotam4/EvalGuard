"""Resolve the actor (who triggered this run) for the audit log.

Identity strategy by environment, in priority order:

1. **CI** — GitHub Actions / GitLab CI / generic env. Uses well-known
   environment variables to capture workflow + ref + run id. Future:
   verify OIDC tokens server-side.
2. **API key** — when ``EVALGUARD_API_KEY_ID`` is set (server tier).
3. **CLI / human** — falls back to OS user + hostname + git config
   email + git HEAD sha if the project directory is a repo.

The identity is intentionally captured from the environment with no
network call, so offline use stays offline.
"""

from __future__ import annotations

import getpass
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Actor:
    actor_id: str
    actor_type: str          # "cli" | "ci" | "api_key" | "system"
    actor_meta: dict[str, Any]


def resolve_actor(project_dir: Path | None = None) -> Actor:
    """Resolve the calling actor.

    ``project_dir`` anchors the ``git`` subprocess so the recorded
    sha/branch reflect the project being evaluated rather than the
    process's ambient cwd (which may be a parent / unrelated repo
    when ``evalguard`` is invoked from outside the project).
    """
    if os.environ.get("EVALGUARD_API_KEY_ID"):
        return _api_key_actor()
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return _github_actor()
    if os.environ.get("GITLAB_CI") == "true":
        return _gitlab_actor()
    if os.environ.get("CI") in {"true", "1"}:
        return _generic_ci_actor()
    return _cli_actor(project_dir)


# ---------------------------------------------------------------------------


def _api_key_actor() -> Actor:
    key_id = os.environ["EVALGUARD_API_KEY_ID"]
    return Actor(
        actor_id=f"api_key:{key_id}",
        actor_type="api_key",
        actor_meta={
            "owner_user_id": os.environ.get("EVALGUARD_USER_ID"),
            "scopes": _split_csv(os.environ.get("EVALGUARD_API_KEY_SCOPES")),
        },
    )


def _github_actor() -> Actor:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return Actor(
        actor_id=f"ci:gh:{repo}#run/{run_id}" if repo else "ci:gh:unknown",
        actor_type="ci",
        actor_meta={
            "ci":          "github_actions",
            "repo":        repo,
            "ref":         os.environ.get("GITHUB_REF"),
            "ref_name":    os.environ.get("GITHUB_REF_NAME"),
            "ref_type":    os.environ.get("GITHUB_REF_TYPE"),
            "workflow":    os.environ.get("GITHUB_WORKFLOW"),
            "job":         os.environ.get("GITHUB_JOB"),
            "run_id":      run_id,
            "run_number":  os.environ.get("GITHUB_RUN_NUMBER"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "actor":       os.environ.get("GITHUB_ACTOR"),
            "event_name":  os.environ.get("GITHUB_EVENT_NAME"),
            "sha":         os.environ.get("GITHUB_SHA"),
            "pr_number":   _pr_from_ref(os.environ.get("GITHUB_REF")),
        },
    )


def _gitlab_actor() -> Actor:
    project = os.environ.get("CI_PROJECT_PATH", "")
    pipeline = os.environ.get("CI_PIPELINE_ID", "")
    return Actor(
        actor_id=f"ci:gitlab:{project}#pipeline/{pipeline}" if project else "ci:gitlab:unknown",
        actor_type="ci",
        actor_meta={
            "ci":         "gitlab_ci",
            "project":    project,
            "pipeline":   pipeline,
            "job":        os.environ.get("CI_JOB_NAME"),
            "ref":        os.environ.get("CI_COMMIT_REF_NAME"),
            "sha":        os.environ.get("CI_COMMIT_SHA"),
            "user_login": os.environ.get("GITLAB_USER_LOGIN"),
            "mr_iid":     os.environ.get("CI_MERGE_REQUEST_IID"),
        },
    )


def _generic_ci_actor() -> Actor:
    return Actor(
        actor_id=f"ci:generic:{socket.gethostname()}",
        actor_type="ci",
        actor_meta={"ci": "generic", "hostname": socket.gethostname()},
    )


def _cli_actor(project_dir: Path | None) -> Actor:
    user = _safe(getpass.getuser, default="unknown")
    host = _safe(socket.gethostname, default="unknown")
    git = _git_metadata(project_dir)
    return Actor(
        actor_id=f"cli:{user}@{host}",
        actor_type="cli",
        actor_meta={"user": user, "hostname": host, **git},
    )


def _git_metadata(project_dir: Path | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sha = _git("rev-parse", "HEAD", cwd=project_dir)
    if sha:
        out["git_sha"] = sha
        out["git_branch"] = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=project_dir)
        out["git_email"]  = _git("config", "user.email", cwd=project_dir)
        out["git_dirty"]  = bool(_git("status", "--porcelain", cwd=project_dir))
    return out


def _git(*args: str, cwd: Path | None = None) -> str | None:
    try:
        result = subprocess.run(
            ("git", *args),
            capture_output=True, text=True, timeout=2, check=False,
            cwd=str(cwd) if cwd is not None else None,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _pr_from_ref(ref: str | None) -> str | None:
    # GH Actions sets refs/pull/{number}/merge for PR events.
    if not ref:
        return None
    parts = ref.split("/")
    if len(parts) >= 4 and parts[1] == "pull":
        return parts[2]
    return None


def _split_csv(value: str | None) -> list[str]:
    return [s.strip() for s in (value or "").split(",") if s.strip()]
