import unittest
from unittest.mock import Mock
from datetime import datetime, timezone

from gh_prs import (
    GitHub, GitHubError, PullRequest, build_report, fetch_pull_requests,
    format_report, has_excluded_label, main, requests_team, to_pull_request,
)


def connection(nodes, more=False, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


def team_request(team="networking", org="anza-xyz"):
    return {"requestedReviewer": {
        "__typename": "Team", "slug": team, "organization": {"login": org},
    }}


def raw_pr(number=1, draft=False, author="community", ready=None):
    return {
        "id": str(number), "number": number, "title": "A PR", "url": "https://example.com/pr",
        "isDraft": draft, "createdAt": "2026-01-01T00:00:00Z", "additions": 3,
        "deletions": 2, "author": {"login": author} if author else None,
        "reviewRequests": connection([team_request()]),
        "labels": connection([]),
        "timelineItems": connection([{"createdAt": date} for date in (ready or [])]),
    }


class ReportTests(unittest.TestCase):
    def test_age_resets_and_creation_fallback(self):
        pr = to_pull_request(raw_pr(ready=["2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z"]))
        now = datetime(2026, 10, 6, tzinfo=timezone.utc)
        self.assertEqual(build_report([pr], now=now)["oldest"][0]["age_days"], 5)
        self.assertEqual(to_pull_request(raw_pr()).ready_since.month, 1)

    def test_author_preserved_without_membership_classification(self):
        pr = to_pull_request(raw_pr(author="Member"))
        self.assertEqual(pr.author, "Member")
        self.assertIsNone(to_pull_request(raw_pr(author=None)).author)
        report = build_report([pr])
        self.assertNotIn("community", report["oldest"][0])
        self.assertNotIn("oldest_community", report)
        self.assertNotIn("Oldest community PRs", format_report(report))

    def test_ranking_limit_and_overlap(self):
        date = lambda day: datetime(2026, 10, day, tzinfo=timezone.utc)
        prs = [
            PullRequest(1, "small", "url", "member", 1, 0, date(5)),
            PullRequest(2, "oldest", "url", "member", 200, 0, date(1)),
            PullRequest(3, "community", "url", "outside", 10, 0, date(2)),
        ]
        report = build_report(prs, limit=1, now=date(6))
        self.assertEqual(report["smallest"][0]["number"], 1)
        self.assertEqual(report["largest"][0]["number"], 2)
        self.assertEqual(report["oldest"][0]["number"], 2)
        self.assertEqual(build_report([])["smallest"], [])
        self.assertEqual(build_report([])["largest"], [])
        report = build_report(prs, limit=2, now=date(6))
        self.assertEqual([pr["number"] for pr in report["largest"]], [2, 3])
        self.assertEqual([pr["number"] for pr in report["smallest"]], [1, 3])

    def test_review_request_pagination_and_org_match(self):
        client = Mock()
        pr = raw_pr()
        pr["reviewRequests"] = connection([team_request(org="other")], True, "next")
        client.graphql.return_value = {"node": {"reviewRequests": connection([team_request()])}}
        self.assertTrue(requests_team(client, pr, "anza-xyz", "networking"))
        self.assertEqual(client.graphql.call_args.kwargs["cursor"], "next")

    def test_pr_pagination_draft_and_team_filter(self):
        client = Mock()
        wrong_team = raw_pr(3)
        wrong_team["reviewRequests"] = connection([team_request("other")])
        client.graphql.side_effect = [
            {"repository": {"pullRequests": connection([raw_pr(1), raw_pr(2, draft=True)], True, "next")}},
            {"repository": {"pullRequests": connection([wrong_team, raw_pr(4, author="member")])}},
        ]
        prs = fetch_pull_requests(client)
        self.assertEqual([pr.number for pr in prs], [1, 4])
        client.request.assert_not_called()
        self.assertEqual(client.graphql.call_args.kwargs["cursor"], "next")

    def test_graphql_partial_errors_fail_instead_of_truncating(self):
        client = GitHub("test")
        client.request = Mock(return_value={"data": {}, "errors": [{"message": "Rate limit exceeded"}]})
        with self.assertRaisesRegex(GitHubError, "Rate limit"):
            client.graphql("query {}")

    def test_excluded_labels_apply_before_ranking(self):
        client = Mock()
        blocked = raw_pr(1)
        blocked["labels"] = connection([{"name": "Blocked"}, {"name": "bug"}])
        allowed = raw_pr(2)
        allowed["labels"] = connection([{"name": "bug"}])
        client.graphql.return_value = {
            "repository": {"pullRequests": connection([blocked, allowed, raw_pr(3)])}
        }
        prs = fetch_pull_requests(client, exclude_labels=["blocked", "DO NOT MERGE"])
        self.assertEqual([pr.number for pr in prs], [2, 3])
        report = build_report(prs)
        self.assertEqual(report["total_matching_prs"], 2)
        for category in ("smallest", "largest", "oldest"):
            self.assertEqual({pr["number"] for pr in report[category]}, {2, 3})

    def test_label_pagination(self):
        client = Mock()
        pr = raw_pr()
        pr["labels"] = connection([{"name": "bug"}], True, "next-label")
        client.graphql.return_value = {
            "node": {"labels": connection([{"name": "blocked"}])}
        }
        self.assertTrue(has_excluded_label(client, pr, {"blocked"}))
        self.assertEqual(client.graphql.call_args.kwargs["cursor"], "next-label")

    def test_cli_label_list(self):
        from unittest.mock import patch
        with (patch("sys.argv", ["gh_prs.py", "--exclude-labels", "blocked", "do not merge",
                                 "--exclude-labels", "wip", "--json"]),
              patch("gh_prs.github_token", return_value="test"),
              patch("gh_prs.fetch_pull_requests", return_value=[]) as fetch,
              patch("builtins.print")):
            self.assertEqual(main(), 0)
        self.assertEqual(fetch.call_args.kwargs["exclude_labels"],
                         ["blocked", "do not merge", "wip"])

    def test_additional_label_lists_order_limit_overlap_and_empty(self):
        from dataclasses import replace
        now = datetime(2026, 10, 6, tzinfo=timezone.utc)
        old = to_pull_request(raw_pr(1, ready=["2026-10-01T00:00:00Z"]), ("Bug", "help wanted"))
        recent = to_pull_request(raw_pr(2, ready=["2026-10-05T00:00:00Z"]), ("bug",))
        unrelated = replace(old, number=3, labels=("feature",))
        report = build_report([recent, unrelated, old], limit=1, now=now,
                              labels=["bug", "BUG", "help wanted", "missing"])
        self.assertEqual(list(report["by_label"]), ["bug", "help wanted", "missing"])
        self.assertEqual([pr["number"] for pr in report["by_label"]["bug"]], [1])
        self.assertEqual(report["by_label"]["help wanted"][0]["number"], 1)
        self.assertEqual(report["by_label"]["missing"], [])
        self.assertEqual(report["by_label"]["bug"][0]["author"], "community")
        self.assertEqual(report["by_label"]["bug"][0]["url"], old.url)
        self.assertIn("Oldest PRs with label: bug\n(+3/-2) #1 A PR (5 days)", format_report(report))
        self.assertIn("Oldest PRs with label: missing\n  No matching PRs.", format_report(report))
        report = build_report([recent, old], now=now, labels=["bug"])
        self.assertEqual([pr["number"] for pr in report["by_label"]["bug"]], [1, 2])

    def test_cli_additional_labels(self):
        from unittest.mock import patch
        with (patch("sys.argv", ["gh_prs.py", "--labels", "bug", "help wanted",
                                 "--labels", "feature", "--limit", "2", "--json"]),
              patch("gh_prs.github_token", return_value="test"),
              patch("gh_prs.fetch_pull_requests", return_value=[]),
              patch("gh_prs.build_report", return_value={}) as build,
              patch("builtins.print")):
            self.assertEqual(main(), 0)
        build.assert_called_once_with([], 2, labels=["bug", "help wanted", "feature"])

    def test_paginated_labels_retained_and_exclusions_apply_to_label_lists(self):
        client = Mock()
        allowed = raw_pr(1)
        allowed["labels"] = connection([{"name": "bug"}], True, "next-label")
        excluded = raw_pr(2)
        excluded["labels"] = connection([{"name": "bug"}, {"name": "blocked"}])
        client.graphql.side_effect = [
            {"repository": {"pullRequests": connection([allowed, excluded])}},
            {"node": {"labels": connection([{"name": "help wanted"}])}},
        ]
        prs = fetch_pull_requests(client, exclude_labels=["blocked"])
        self.assertEqual(prs[0].labels, ("bug", "help wanted"))
        report = build_report(prs, labels=["bug", "help wanted"])
        for items in report["by_label"].values():
            self.assertEqual([pr["number"] for pr in items], [1])


if __name__ == "__main__":
    unittest.main()
