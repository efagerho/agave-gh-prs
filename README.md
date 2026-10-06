# PRs for anza-xyz/networking

Pulls non-draft PRs in `anza-xyz/agave` with an outstanding review request for `anza-xyz/networking`.
PRs are then sorted into different lists by various criteria.

- Top-N smallest PRs, by additions + deletions, ascending.
- Top-N largest PRs, by additions + deletions, descending.
- Top-N PRs by age, oldest first.

Age starts at the latest transition out of draft, or creation time if the PR was never a draft. Use `--limit N` to set the maximum number of PRs in each list (default: 5).

## Run

```sh
python3 gh_prs.py
python3 gh_prs.py --json
python3 gh_prs.py --limit 10
python3 gh_prs.py --exclude-labels blocked "do not merge"
python3 gh_prs.py --labels community bug "help wanted"
```

`--exclude-labels` excludes PRs with any of the given labels from all lists and the matching PR count, in both text and JSON output. Matching is case-insensitive and uses the whole label name. Quote names containing spaces.

`--labels` adds a separate list for each requested label, ordered by decreasing age (oldest first). Each list uses `--limit` (default: 5) and the same team, non-draft, and exclusion filters. Label matching is case-insensitive; repeated labels produce only one list. PRs can appear in multiple lists. JSON includes these additional lists under `by_label` and preserves author and PR URL fields.