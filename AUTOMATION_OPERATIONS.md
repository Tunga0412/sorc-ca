# SORCTracks automation operations

The hosted workflows are intended to keep SORCTracks Live and the historical tool running without the original updater computer.

## Normal operation

- Live updates run daily from the current AHS public disruption page.
- Historical updates run monthly on the first day of each month.
- Public health checks run every six hours.
- A failed health check opens or updates the GitHub issue titled "SORC automation health check failure". When the public pages recover, the issue is closed automatically.

## If a workflow fails

1. Open the failed workflow run and read the failed step.
2. Check the health-check failure issue for the latest observed error.
3. Do not publish a candidate manually unless the source and output have been reviewed.
4. If AHS changed its page structure, update the parser and run the Live workflow manually before relying on the next schedule.
5. If the historical input assumptions changed, create a new sanitized historical bundle, publish it as a versioned release asset, and update BUNDLE_URL in .github/workflows/sorc-historical-update.yml.

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
