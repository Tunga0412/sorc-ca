# Hosted SORCTracks Live updater

The Live workflow runs from the repository rather than the personal updater computer. It uses the current AHS disruption page, the checked-in historical tool as the stable site registry, and the pinned Python environment in this directory.

The workflow is scheduled daily and can also be started manually from GitHub Actions. It generates a candidate file, checks its data contract and freshness, then commits only the accepted Live output. A failed scrape, unmapped site, unparsed schedule, fixture source, map-key error, or stale candidate stops the job before publication.

The Windows updater remains a fallback until the hosted workflow has completed a successful production run.
