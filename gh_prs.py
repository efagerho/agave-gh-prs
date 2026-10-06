#!/usr/bin/env python3
"""Rank open PRs awaiting review from a GitHub team (Python 3.10+)."""

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, path, body=None):
        request = Request(
            f"https://api.github.com/{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "networking-pr-report",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                message = json.load(error).get("message", error.reason)
            except (ValueError, AttributeError):
                message = error.reason
            raise GitHubError(f"GitHub HTTP {error.code}: {message}") from error
        except URLError as error:
            raise GitHubError(f"Could not reach GitHub: {error.reason}") from error

    def graphql(self, query, **variables):
        response = self.request("graphql", {"query": query, "variables": variables})
        if response.get("errors"):
            raise GitHubError("; ".join(e["message"] for e in response["errors"]))
        return response["data"]


def github_token():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, check=True, timeout=15,
        )
        if result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    raise GitHubError("Set GH_TOKEN or GITHUB_TOKEN, or authenticate with gh auth login.")


REVIEW_FIELDS = """
    nodes {
      requestedReviewer {
        __typename
        ... on Team { slug organization { login } }
      }
    }
    pageInfo { hasNextPage endCursor }
"""

PRS_QUERY = """
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequests(states: OPEN, first: 50, after: $cursor,
                 orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes {
        id number title url isDraft createdAt additions deletions
        author { login }
        labels(first: 100) {
          nodes { name }
          pageInfo { hasNextPage endCursor }
        }
        reviewRequests(first: 100) { REVIEW_FIELDS }
        timelineItems(last: 1, itemTypes: [READY_FOR_REVIEW_EVENT]) {
          nodes { ... on ReadyForReviewEvent { createdAt } }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""".replace("REVIEW_FIELDS", REVIEW_FIELDS)

REVIEWS_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on PullRequest {
      reviewRequests(first: 100, after: $cursor) { REVIEW_FIELDS }
    }
  }
}
""".replace("REVIEW_FIELDS", REVIEW_FIELDS)

LABELS_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on PullRequest {
      labels(first: 100, after: $cursor) {
        nodes { name }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def pull_request_labels(client, pr):
    connection = pr["labels"]
    labels = []
    while True:
        labels.extend(label["name"] for label in connection["nodes"])
        if not connection["pageInfo"]["hasNextPage"]:
            return tuple(labels)
        data = client.graphql(
            LABELS_QUERY, id=pr["id"], cursor=connection["pageInfo"]["endCursor"]
        )
        connection = data["node"]["labels"]


def has_excluded_label(client, pr, excluded_labels):
    return bool(excluded_labels) and any(
        label.casefold() in excluded_labels for label in pull_request_labels(client, pr)
    )


def requests_team(client, pr, org, team):
    connection = pr["reviewRequests"]
    while True:
        for node in connection["nodes"]:
            reviewer = node.get("requestedReviewer") or {}
            if (reviewer.get("__typename") == "Team"
                    and reviewer["slug"].casefold() == team.casefold()
                    and reviewer["organization"]["login"].casefold() == org.casefold()):
                return True
        if not connection["pageInfo"]["hasNextPage"]:
            return False
        data = client.graphql(
            REVIEWS_QUERY, id=pr["id"], cursor=connection["pageInfo"]["endCursor"]
        )
        connection = data["node"]["reviewRequests"]


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    url: str
    author: str | None
    additions: int
    deletions: int
    ready_since: datetime
    labels: tuple[str, ...] = ()

    @property
    def size(self):
        return self.additions + self.deletions


def to_pull_request(pr, labels=()):
    events = pr["timelineItems"]["nodes"]
    ready_since = max(
        (parse_time(event["createdAt"]) for event in events),
        default=parse_time(pr["createdAt"]),
    )
    author = pr["author"]["login"] if pr["author"] else None
    return PullRequest(
        pr["number"], pr["title"], pr["url"], author,
        pr["additions"], pr["deletions"], ready_since,
        labels,
    )


def fetch_pull_requests(client, org="anza-xyz", repo="agave", team="networking",
                        exclude_labels=()):
    excluded_labels = {label.casefold() for label in exclude_labels}
    result = []
    cursor = None
    while True:
        data = client.graphql(PRS_QUERY, owner=org, repo=repo, cursor=cursor)
        if data["repository"] is None:
            raise GitHubError(f"Repository {org}/{repo} is unavailable.")
        connection = data["repository"]["pullRequests"]
        for pr in connection["nodes"]:
            if not pr["isDraft"] and requests_team(client, pr, org, team):
                labels = pull_request_labels(client, pr)
                if not any(label.casefold() in excluded_labels for label in labels):
                    result.append(to_pull_request(pr, labels))
        if not connection["pageInfo"]["hasNextPage"]:
            return result
        cursor = connection["pageInfo"]["endCursor"]


def rank_pull_requests(prs, limit=5):
    oldest = sorted(prs, key=lambda pr: (pr.ready_since, pr.number))
    return {
        "smallest": sorted(prs, key=lambda pr: (pr.size, pr.ready_since, pr.number))[:limit],
        "largest": sorted(prs, key=lambda pr: (-pr.size, pr.ready_since, pr.number))[:limit],
        "oldest": oldest[:limit],
    }


def build_report(prs, limit=5, now=None, labels=()):
    now = now or datetime.now(timezone.utc)

    def serialize(items):
        return [
            dict(asdict(pr), ready_since=pr.ready_since.isoformat(),
                 lines_changed=pr.size,
                 age_days=round((now - pr.ready_since).total_seconds() / 86400, 2))
            for pr in items
        ]
    lists = {category: serialize(items) for category, items in rank_pull_requests(prs, limit).items()}
    if labels:
        oldest = sorted(prs, key=lambda pr: (pr.ready_since, pr.number))
        by_label = {}
        seen = set()
        for label in labels:
            normalized = label.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            matching = [pr for pr in oldest if normalized in {name.casefold() for name in pr.labels}]
            by_label[label] = serialize(matching[:limit])
        lists["by_label"] = by_label
    return {"generated_at": now.isoformat(), "total_matching_prs": len(prs), **lists}


def format_report(report):
    lines = [f"Open non-draft PRs awaiting team review: {report['total_matching_prs']}"]
    sections = [
        ("Smallest PRs", report["smallest"]), ("Largest PRs", report["largest"]),
        ("Oldest PRs", report["oldest"]),
    ]
    sections.extend((f"Oldest PRs with label: {label}", items)
                    for label, items in report.get("by_label", {}).items())
    for title, items in sections:
        lines.extend(["", title])
        if not items:
            lines.append("  No matching PRs.")
        for pr in items:
            title = " ".join(pr["title"].split())
            lines.append(
                f"(+{pr['additions']}/-{pr['deletions']}) "
                f"#{pr['number']} {title} ({int(pr['age_days'])} days)"
            )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="anza-xyz")
    parser.add_argument("--repo", default="agave")
    parser.add_argument("--team", default="networking")
    parser.add_argument("--limit", type=int, default=5,
                        help="Maximum number of PRs in each list (default: 5)")
    parser.add_argument("--json", action="store_true", help="Output structured JSON")
    parser.add_argument("--exclude-labels", nargs="+", action="extend", default=[],
                        metavar="LABEL", help="Exclude PRs with any listed label (case-insensitive)")
    parser.add_argument("--labels", nargs="+", action="extend", default=[],
                        metavar="LABEL", help="Add an oldest-first PR list for each label")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    try:
        prs = fetch_pull_requests(GitHub(github_token()), args.org, args.repo, args.team,
                                 exclude_labels=args.exclude_labels)
        report = build_report(prs, args.limit, labels=args.labels)
        print(json.dumps(report, indent=2) if args.json else format_report(report))
    except (GitHubError, TimeoutError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
