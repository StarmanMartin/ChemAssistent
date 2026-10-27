"""chemotion_api bug reports in developer mode (no GTK, stdlib).

With `developer_mode` on, the agent reports every bug it finds in
chemotion_api with its `report_api_bug` tool (agent.py, mcp_bridge.py). The
report gets an environment section the agent cannot get wrong: the date, the
AI model, the Chemotion instance (URL and the version from its public /about
page) and the installed chemotion_api branch or tag with its commit.

Every report is saved as Markdown in ISSUES_DIR. With a `github_token` it is
also created as an issue of the chemotionapi repository (unless an open issue
has the same title); without one the agent gets a link to GitHub's
new-issue form with title and body filled in, for the user to submit.
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from importlib import metadata

import api_versions
import lessons
from config import AGENT_LABELS, CLI_AGENTS, STATE_DIR, Config, model_name


ISSUES_DIR = STATE_DIR / "issues"
REPO = api_versions.REPO_URL.removeprefix("https://github.com/").removesuffix(".git")
API_URL = f"https://api.github.com/repos/{REPO}/issues"
NEW_ISSUE_URL = f"https://github.com/{REPO}/issues/new"
# Browsers and GitHub refuse very long URLs: the prefilled body is clipped.
MAX_URL_BODY_CHARS = 6000
MAX_TITLE_CHARS = 120
MAX_BODY_CHARS = 20000
TIMEOUT_S = 15


def eln_version(config: Config) -> str:
    """The Chemotion ELN's version and revision from its public /about page."""
    context = None if config.verify_tls else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(f"{config.url}/about", timeout=TIMEOUT_S, context=context) as response:
            page = response.read(500_000).decode("utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        return f"unknown (/about: {exc})"
    version = re.search(r"Version:\s*([^<\s]+)", page)
    revision = re.search(r"Current revision:\s*([0-9a-f]{7,40})", page)
    if not version:
        return "unknown (/about names no version)"
    return version.group(1) + (f", revision {revision.group(1)[:9]}" if revision else "")


def api_version() -> str:
    """The installed chemotion_api: branch or tag, commit and package version."""
    label = api_versions.installed_label() or "unknown"
    commit = lessons.api_commit()
    try:
        package = metadata.version("chemotion_api")
    except metadata.PackageNotFoundError:
        package = ""
    details = ", ".join(part for part in (commit and f"commit {commit}", package and f"package {package}") if part)
    return f"{label} ({details})" if details else label


def model_label(config: Config) -> str:
    """The model and who runs it, e.g. "alias.complex-local (Built-in agent · OpenAI-compatible API,
    https://ki-toolbox.scc.kit.edu/api/v1)"."""
    where = AGENT_LABELS.get(config.agent, config.agent)
    if config.agent not in CLI_AGENTS and config.ai_url:
        where += f", {config.ai_url}"
    return f"{model_name(config.ai_model)} ({where})"


def environment(config: Config, model: str) -> list[tuple[str, str]]:
    return [
        ("Date", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")),
        ("AI model", model),
        ("Chemotion instance", f"{config.url} (version {eln_version(config)})"),
        ("chemotion_api", api_version()),
    ]


def compose(description: str, rows: list[tuple[str, str]]) -> str:
    table = "\n".join(f"| {key} | {value.replace('|', '/')} |" for key, value in rows)
    return (f"{description.strip()}\n\n---\n\n**Environment** (reported by ChemAssistent in developer mode)\n\n"
            f"| | |\n|---|---|\n{table}\n")


def save_draft(title: str, body: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "issue"
    path = ISSUES_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.md"
    ISSUES_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n{body}", encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def _github(token: str, url: str, data: dict | None = None):
    request = urllib.request.Request(url, data=json.dumps(data).encode("utf-8") if data is not None else None,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28",
                                              "Content-Type": "application/json",
                                              "User-Agent": "ChemAssistent"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def _same(a: str, b: str) -> bool:
    return re.sub(r"\W+", " ", a).strip().lower() == re.sub(r"\W+", " ", b).strip().lower()


def find_open(token: str, title: str) -> dict | None:
    """An open issue (not a pull request) with the same title, or None."""
    for page in range(1, 4):
        issues = _github(token, f"{API_URL}?state=open&per_page=100&page={page}")
        for issue in issues:
            if "pull_request" not in issue and _same(str(issue.get("title") or ""), title):
                return issue
        if len(issues) < 100:
            break
    return None


def new_issue_link(title: str, body: str) -> tuple[str, bool]:
    """GitHub's new-issue form with title and body; True if the body was clipped."""
    clipped = len(body) > MAX_URL_BODY_CHARS
    if clipped:
        body = body[:MAX_URL_BODY_CHARS] + "\n\n…(clipped, the full report is in the saved file)"
    return f"{NEW_ISSUE_URL}?{urllib.parse.urlencode({'title': title, 'body': body})}", clipped


def report(config: Config, model: str, title: str, description: str) -> str:
    """Save (and with a token create) the issue; the text tells the agent what happened."""
    title = " ".join(title.split())[:MAX_TITLE_CHARS]
    if not title or not description.strip():
        return "Give a title and a description (what you did, the code, what you expected, what happened)."
    body = compose(description[:MAX_BODY_CHARS], environment(config, model))
    draft = save_draft(title, body)
    problem = ""
    if config.github_token:
        try:
            existing = find_open(config.github_token, title)
            if existing:
                return (f"An open issue with this title exists already: #{existing.get('number')} "
                        f"{existing.get('html_url')}. Nothing was created; tell the user (with the link). "
                        f"Saved locally as {draft}.")
            issue = _github(config.github_token, API_URL, {"title": title, "body": body})
            return (f"Issue #{issue.get('number')} created: {issue.get('html_url')}. Tell the user (with the link). "
                    f"Saved locally as {draft}.")
        except urllib.error.HTTPError as exc:
            problem = f"GitHub refused the issue (HTTP {exc.code}: {exc.reason}). "
        except (OSError, ValueError) as exc:
            problem = f"GitHub could not be reached ({exc}). "
    else:
        problem = "No GitHub token is set in the settings, so the issue was not created. "
    link, clipped = new_issue_link(title, body)
    return (f"{problem}The report was saved as {draft}. Give the user this link to submit it on GitHub "
            f"(title and body are filled in{'; the body is clipped there' if clipped else ''}): {link}")

