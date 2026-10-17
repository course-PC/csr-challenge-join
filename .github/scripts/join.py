#!/usr/bin/env python3
"""
Join flow: "Join" issue -> private group repository created from the template.

  1. redact the course code from the issue, validate deadline and code
     (skipped when an instructor reopens the issue to retry it)
  2. validate teammates: existing GitHub users, group size
  3. reject members already in a group, enforce the group cap
  4. create ORG/csr-<opener> from the template (or resume a half-done one with the same members),
     tag it, record the members in its description
  5. invite every member as collaborator (push), reply on the issue, close and lock it

All input from the issue is untrusted: the body is fetched through the API (never through
the workflow environment, which public logs print), usernames are validated by regex,
and gh is called without a shell.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

# ----------------------------------------------------------------------------
# Configuration (from the workflow)
# ----------------------------------------------------------------------------

ENV = os.environ
ORG = ENV["ORG"]
JOIN_REPO = ENV["JOIN_REPO"]
ISSUE = ENV["ISSUE_NUMBER"]
AUTHOR = ENV["ISSUE_AUTHOR"]
TEMPLATE = ENV.get("TEMPLATE", "csr-challenge")
TOPIC = ENV.get("TOPIC", "csr-group")
MAX_MEMBERS = int(ENV.get("MAX_MEMBERS") or 3)
MAX_GROUPS = int(ENV.get("MAX_GROUPS") or 0)  # 0 = no cap
DEADLINE = ENV.get("JOIN_DEADLINE", "").strip()
CODE = ENV.get("JOIN_CODE", "").strip()
ADMIN_TOKEN = ENV.get("ADMIN_TOKEN", "")
# an instructor reopening the issue retries it: the code is already redacted, skip code and deadline
RETRY = ENV.get("EVENT_ACTION") == "reopened" and ENV.get("SENDER", "").lower() != AUTHOR.lower()
ISSUE_TOKEN = ENV.get("ISSUE_TOKEN", "")

LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
MEMBERS_RE = re.compile(r"members:\s*(.*)$")


class UserError(Exception):
    """Problem with the request: the student fixes it and opens a new issue."""


class GhError(Exception):
    """gh call failed."""


# ----------------------------------------------------------------------------
# GitHub helpers
# ----------------------------------------------------------------------------

def gh(args, token, stdin=None):
    p = subprocess.run(["gh", *args], env=dict(ENV, GH_TOKEN=token), input=stdin,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode:
        raise GhError(f"gh {' '.join(args[:3])}: {p.stderr.strip()}")
    return p.stdout


def api(path, method="GET", fields=(), raw_fields=(), token=None, jq=None, paginate=False):
    args = ["api", "-X", method, path]
    for k, v in fields:
        args += ["-f", f"{k}={v}"]
    for k, v in raw_fields:
        args += ["-F", f"{k}={v}"]
    if paginate:
        args.append("--paginate")
    if jq:
        args += ["-q", jq]
    return gh(args, token or ADMIN_TOKEN)


def user(login):
    """Canonical login of an existing GitHub user, or None."""
    try:
        u = json.loads(api(f"users/{login}"))
    except GhError:
        return None
    return u["login"] if u.get("type") == "User" else None


def repo_members(name):
    """Members recorded in ORG/name's description, None if the repository does not exist."""
    try:
        r = json.loads(api(f"repos/{ORG}/{name}"))
    except GhError:
        return None
    m = MEMBERS_RE.search(r.get("description") or "")
    return m.group(1).lower().split() if m else []


def existing_groups():
    """{repo name: [members]} for every group repository."""
    out = api(f"orgs/{ORG}/repos?type=all&per_page=100", paginate=True,
              jq=f'.[] | select(.topics | index("{TOPIC}")) | {{name, description}}')
    groups = {}
    for line in out.splitlines():
        if line.strip():
            r = json.loads(line)
            m = MEMBERS_RE.search(r.get("description") or "")
            groups[r["name"].lower()] = m.group(1).lower().split() if m else []
    return groups


def issue(cmd, *args):
    return gh(["issue", cmd, ISSUE, "--repo", JOIN_REPO, *args], ISSUE_TOKEN)


# ----------------------------------------------------------------------------
# Issue parsing
# ----------------------------------------------------------------------------

BODY = ""


def fetch_body():
    global BODY
    BODY = json.loads(gh(["api", f"repos/{JOIN_REPO}/issues/{ISSUE}"], ISSUE_TOKEN)).get("body") or ""


def section(label):
    m = re.search(rf"^###\s*{re.escape(label)}\s*$(.*?)(?=^###\s|\Z)", BODY, re.M | re.S)
    text = m.group(1).strip() if m else ""
    return "" if text == "_No response_" else text


def redact_code():
    if not section("Course code"):
        return
    body = re.sub(r"(^###\s*Course code\s*$)(.*?)(?=^###\s|\Z)", r"\1\n\n_redacted_\n\n", BODY,
                  flags=re.M | re.S)
    gh(["api", "-X", "PATCH", f"repos/{JOIN_REPO}/issues/{ISSUE}", "-f", f"body={body}"], ISSUE_TOKEN)


def teammates():
    logins = []
    for tok in re.split(r"[\s,;]+", section("Teammates")):
        tok = tok.strip().rstrip("/").split("/")[-1].lstrip("@")
        if not tok:
            continue
        if not LOGIN_RE.fullmatch(tok):
            raise UserError(f"`{tok[:40]}` is not a valid GitHub username.")
        logins.append(tok)
    return logins


# ----------------------------------------------------------------------------
# Join
# ----------------------------------------------------------------------------

def join():
    # deadline, code
    if not RETRY and DEADLINE and datetime.now(timezone.utc) > datetime.fromisoformat(DEADLINE.replace("Z", "+00:00")):
        raise UserError("Registration is closed. Contact the instructors.")
    if not RETRY and CODE and section("Course code").strip().lower() != CODE.lower():
        raise UserError("Wrong course code.")

    # members: opener first, canonical logins, no duplicates
    members = [AUTHOR]
    for t in teammates():
        login = user(t)
        if login is None:
            raise UserError(f"GitHub user `{t}` not found.")
        if login.lower() not in (m.lower() for m in members):
            members.append(login)
    if len(members) > MAX_MEMBERS:
        raise UserError(f"{len(members)} members (you included), at most {MAX_MEMBERS} allowed.")

    # own repository: absent, or half-done by an earlier run with the same members (resume)
    name = f"csr-{AUTHOR.lower()}"
    own = repo_members(name)
    if own is not None and own != [m.lower() for m in members]:
        raise UserError(f"You already created a group: https://github.com/{ORG}/{name}")

    if own is None:
        # other groups, cap
        groups = existing_groups()
        for m in members:
            for g, ms in groups.items():
                if g != name and m.lower() in ms:
                    raise UserError(f"@{m} is already in group https://github.com/{ORG}/{g}")
        if MAX_GROUPS and len(groups) >= MAX_GROUPS:
            raise UserError("The maximum number of groups has been reached. Contact the instructors.")

        # repository
        print(f"creating {ORG}/{name} for {members}")
        api(f"repos/{ORG}/{TEMPLATE}/generate", "POST",
            fields=[("owner", ORG), ("name", name), ("description", "members: " + " ".join(members))],
            raw_fields=[("private", "true")])
        for _ in range(30):
            if repo_members(name) is not None:
                break
            time.sleep(2)
        else:
            raise GhError(f"{ORG}/{name} not available after creation")
    else:
        print(f"resuming {ORG}/{name} for {members}")
    api(f"repos/{ORG}/{name}/topics", "PUT", fields=[("names[]", TOPIC)])

    # invitations
    for m in members:
        try:
            api(f"repos/{ORG}/{name}/collaborators/{m}")  # 204: already has access (e.g. an org owner)
            continue
        except GhError:
            pass
        api(f"repos/{ORG}/{name}/collaborators/{m}", "PUT", fields=[("permission", "push")])

    url = f"https://github.com/{ORG}/{name}"
    return (
        f"✅ Repository created: {url}\n\n"
        f"Invited: {' '.join('@' + m for m in members)}\n\n"
        f"Every member must **accept the invitation within 7 days**: check your e-mail "
        f"or open {url}/invitations\n\n"
        f"Next: clone the repository, fill in the group header at the top of `my_kernel.cu` "
        f"(`Surname; Name; Student ID`, one line per member) and push. "
        f"Every push is checked automatically (Actions tab of your repository)."
    )


def main():
    fetch_body()
    try:
        redact_code()
    except GhError as e:
        print(f"warning: could not redact the course code: {e}")

    try:
        msg = join()
        issue("comment", "--body", msg)
        issue("close", "--reason", "completed")
        issue("lock")
    except UserError as e:
        issue("comment", "--body", f"❌ {e}\n\nFix it and open a new issue.")
        issue("close", "--reason", "not planned")
    except Exception as e:  # noqa: BLE001 - anything else is on our side
        print(f"error: {e}", file=sys.stderr)
        detail = str(e).replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"::error title=join failed::{detail}")
        issue("comment", "--body", "⚠️ Something went wrong on our side. "
                                   "The instructors have been notified; no need to open a new issue.\n\n"
                                   "_Instructors: fix the cause, then close and reopen this issue to retry._")
        sys.exit(1)


if __name__ == "__main__":
    main()
