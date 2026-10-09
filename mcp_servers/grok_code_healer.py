"""Grok Code Healer MCP server.

A review-first code-healing server for a checked-out repository.  It can inspect
an allowed repository, run bounded project checks, and ask Grok for a structured
remediation proposal. It cannot push, merge, change deployment settings, or
write to a protected/default branch. Applying a proposed patch is opt-in and is
blocked unless explicit write mode is enabled in the server environment.

Run locally with:
  XAI_API_KEY=... CODE_HEAL_ALLOWED_ROOT=/workspace \
    python mcp_servers/grok_code_healer.py
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from mcp.server.fastmcp import FastMCP
from openai import OpenAI


mcp = FastMCP("Grok Code Healer")
MAX_OUTPUT = 18_000
DEFAULT_MODEL = os.getenv("GROK_CODE_HEAL_MODEL", "grok-4.7")


class CodeHealerError(ValueError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    output: str


def _allowed_root() -> Path:
    return Path(os.getenv("CODE_HEAL_ALLOWED_ROOT", Path.cwd())).resolve()


def resolve_repo(repo_path: str) -> Path:
    candidate = Path(repo_path).resolve()
    root = _allowed_root()
    if root != candidate and root not in candidate.parents:
        raise CodeHealerError("Repository is outside CODE_HEAL_ALLOWED_ROOT.")
    if not (candidate / ".git").is_dir():
        raise CodeHealerError("Repository must be a Git working tree.")
    return candidate


def _run(repo: Path, command: list[str], timeout: int = 120) -> CommandResult:
    try:
        result = subprocess.run(
            command,
            cwd=repo,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(command, 124, f"Timed out after {timeout}s: {exc}")
    output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
    return CommandResult(command, result.returncode, output[:MAX_OUTPUT])


def inspect_repository(repo_path: str) -> dict[str, Any]:
    """Collect bounded, local diagnostics with no model or network call."""
    repo = resolve_repo(repo_path)
    branch = _run(repo, ["git", "branch", "--show-current"])
    status = _run(repo, ["git", "status", "--short"])
    diff_check = _run(repo, ["git", "diff", "--check"])
    log = _run(repo, ["git", "log", "-1", "--oneline"])
    diagnostics: list[CommandResult] = [branch, status, diff_check, log]
    if (repo / "package.json").exists():
        diagnostics.append(_run(repo, ["npm", "run", "check", "--if-present"], timeout=180))
    elif (repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists():
        diagnostics.append(_run(repo, ["python3", "-m", "pytest", "-q"], timeout=180))
    return {
        "repo": str(repo),
        "branch": branch.output.strip(),
        "default_branch_blocked": branch.output.strip() in {"main", "master"},
        "diagnostics": [asdict(item) for item in diagnostics],
    }


def _client() -> OpenAI:
    api_key = os.getenv("XAI_API_KEY", "").strip()
    if not api_key:
        raise CodeHealerError("XAI_API_KEY is not configured on the MCP host.")
    return OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")


def _parse_json(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidate = fenced.group(1) if fenced else text.strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise CodeHealerError("Grok did not return the required JSON remediation plan.") from exc
    if not isinstance(value, dict):
        raise CodeHealerError("Grok remediation output must be a JSON object.")
    return value


def review_with_grok(diagnostics: dict[str, Any], focus: str) -> dict[str, Any]:
    """Ask Grok for analysis only; does not mutate the repository."""
    prompt = """You are a senior code reviewer. Analyze only the supplied local diagnostic data.
Return a single JSON object with exactly these keys: summary (string), severity
(one of low/medium/high/critical), root_causes (array of strings), proposed_changes
(array of objects with file, change, rationale), tests_to_run (array of strings),
risk_notes (array of strings), and requires_human_approval (boolean).
Do not suggest secrets, package upgrades without a reason, schema destruction,
deployment changes, pushes, merges, or edits to a default branch. Prefer the
smallest safe fix. If diagnostics are insufficient, say so and propose inspection
steps instead of inventing a fix.

Focus: """ + focus + "\n\nDiagnostics:\n" + json.dumps(diagnostics, ensure_ascii=False)
    response = _client().responses.create(
        model=DEFAULT_MODEL,
        input=prompt,
        max_output_tokens=3000,
    )
    text = getattr(response, "output_text", "")
    plan = _parse_json(text)
    plan["model"] = DEFAULT_MODEL
    plan["mode"] = "review_only"
    return plan


@mcp.tool()
def audit_repository(repo_path: str, focus: str = "Find the smallest safe fix for failing checks.") -> dict[str, Any]:
    """Run local diagnostics in an allowlisted repository and return a Grok remediation plan."""
    diagnostics = inspect_repository(repo_path)
    return {"diagnostics": diagnostics, "remediation": review_with_grok(diagnostics, focus)}


@mcp.tool()
def inspect_failure(repo_path: str) -> dict[str, Any]:
    """Return local diagnostics only, suitable when model analysis is not desired."""
    return inspect_repository(repo_path)


@mcp.tool()
def prepare_patch_request(repo_path: str, issue_description: str) -> dict[str, Any]:
    """Create a review-only change plan; no file, Git, or deployment mutation occurs."""
    diagnostics = inspect_repository(repo_path)
    plan = review_with_grok(diagnostics, issue_description)
    return {
        "repo": diagnostics["repo"],
        "branch": diagnostics["branch"],
        "write_allowed": os.getenv("GROK_CODE_HEAL_ALLOW_WRITES", "false").lower() == "true",
        "next_step": "Review the plan, edit on a non-default branch, run tests, then open a PR for human review.",
        "remediation": plan,
    }


if __name__ == "__main__":
    mcp.run()
