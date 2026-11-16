# SORCTracks automation operations

The hosted workflows are intended to keep SORCTracks Live and the historical tool running without the original updater computer.

## Normal operation

- Live updates run daily from the current AHS public disruption page.
- Historical updates run monthly on the first day of each month.
- Public health checks run every six hours.
- A failed historical publisher opens or updates the GitHub issue titled "SORCTracks historical update failure". A failed public health check opens or updates the GitHub issue titled "SORC automation health check failure". When the relevant checks recover, the issues are closed automatically.

## If a workflow fails

1. Open the failed workflow run and read the failed step.
2. Check the health-check failure issue for the latest observed error.
3. Confirm that the previous accepted publication remains in place. A failed run does not replace it.
4. Do not publish a candidate manually unless the source and output have been reviewed.
5. If AHS changed its page structure, update the parser and run the Live workflow manually before relying on the next schedule.
6. If the historical input assumptions changed, create a new sanitized historical bundle, publish it as a versioned release asset, and update BUNDLE_URL in .github/workflows/sorc-historical-update.yml.

## Ownership and recovery

Keep at least one additional repository maintainer with access to GitHub Actions, releases, repository settings, and the sorc.ca hosting configuration. Keep the domain renewal account and release-bundle location documented outside the repository as well.

The historical bundle is intentionally versioned. This preserves reproducibility, but it is not self-refreshing. Parser changes, manual decisions, routing data, and source baselines require a deliberate bundle refresh.

The hosted wrapper seeds the runner-local baseline from the checked-out accepted historical publication on every run, so a fresh runner does not require a manually prepared cache. It also applies a narrow compatibility repair for known omissions in older pinned bundles before importing them. These safeguards do not replace deliberate bundle refreshes when the parser, source assumptions, or routing data change.

## Dependency updates

automation/requirements.lock.txt is the tested hosted environment lock. Update it deliberately, run both publisher workflows, and retain the direct requirements file as the review-oriented dependency list.


## SORCShortages monthly update

The monthly Shortages workflow uses the documented Health Product Shortages Canada API. Configure these repository secrets before the first successful run:

- `SORCSHORTAGES_API_EMAIL`
- `SORCSHORTAGES_API_PASSWORD`

The workflow pulls the complete report stream, rebuilds the existing episode model, validates report counts, report types, date metadata, and monthly series, then commits the two data files only after validation. A failed pull leaves the last accepted dataset in place.

The source account has a 90-day password reset requirement. The API secret must therefore be rotated before expiry. The public health workflow will continue to identify a stale Shortages dataset if a monthly run stops succeeding.

## SORCShortages 1.1 verification and recovery

The main-only Shortages publisher uses Ubuntu 24.04, Python 3.13.16 and `sorcshortages/requirements.txt`. Regression tests and an independent full source-to-output audit precede publication. The accepted publication includes CSV/JSON/JavaScript, its original compressed source and a source-audit receipt. The validated Pages workflow is triggered after the publisher succeeds and verifies those exact files again before creating an artifact.

An incomplete census, duplicate report, unrecognized status/type, inconsistent export or loss exceeding the 99% retention floor refuses publication. Added/removed report keys are retained in the source-audit receipt. A source-reported date inconsistency is preserved and flagged; it is not silently corrected.

The provider can block automated public exports at its security layer. Such a refusal is reported explicitly and leaves the accepted dataset in place. A maintainer can download a complete original provider ZIP in a regular browser and run `python -m automation.shortages_monthly --source csv-zip --input-export /path/to/original-export.zip`; the same source audit, census checks and provenance retention apply. A retained source can be replayed with `--source snapshot --input-snapshot /path/to/original.json.gz`. Its original snapshot date and capture interval are preserved.

Before a manual data commit, run `python -B tests/test_shortages.py` and the Pages gate against the complete checkout with an output directory outside that checkout. Review source-audit additions/removals and retain raw snapshots. Do not change SORCTracks data to repair Shortages. The source-audit receipt establishes consistency with captured reports, not clinical accuracy or a complete source-revision archive.
