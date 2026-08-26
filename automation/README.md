# Hosted SORCTracks automation

## Live updater

The Live workflow runs from the repository rather than the personal updater computer. It uses the current AHS disruption page, the checked-in historical tool as the stable site registry, and the pinned Python environment in this directory.

The workflow is scheduled daily and can also be started manually from GitHub Actions. It generates a candidate file, checks its data contract and freshness, then commits only the accepted Live output. A failed scrape, unmapped site, unparsed schedule, fixture source, map-key error, or stale candidate stops the job before publication.

## Historical updater

The historical workflow downloads the versioned input bundle from the GitHub release tagged `historical-bundle-2026-08-26`. It reconstructs the required local paths inside the hosted runner, checks whether the previous completed month is already represented, and runs the existing full historical pipeline and release QA before publishing.

The historical bundle is intentionally separate from the website files because the source corpus and derived outputs are much larger than the public pages. Refresh the release bundle when parser logic, manual decisions, routing data, or baselines change.

## Independent health check

The public health workflow checks both pages every six hours for missing data, stale Live output, fixture data, and known map errors.

The Windows updater remains available as a fallback while the hosted workflows are being validated.
