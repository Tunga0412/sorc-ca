# SORC website package

This folder contains the deployable SORC webpages and the separately maintained tool files they embed.

## Main pages

- `index.html` - research and publications landing page
- `about.html` - SORC members and approach
- `projects.html` - ongoing projects
- `jip.html` - Junior Investigator Program
- `standards.html` - authorship and collaboration standards
- `sorctracks.html` - SORCTracks hosting page
- `methods.html` - SORCTracks methods
- `sorcshortages.html` - SORCShortages hosting page

## Tool files

- `sorctracks_tool.html` - SORCTracks interactive tool
- `sorctracks_live.html` - SORCTracks Live page with the current AHS public snapshot
- `sorcshortages_tool.html` - SORCShortages interactive tool
- `data/consolidated.js` - SORCShortages tool data

The hosting pages load the tools through relative iframe paths. Keep the tool files and the `data` folder in this same directory when deploying.

`SORC_LOGO.png` supplies the favicon assets referenced by the webpages. `robots.txt` keeps the standalone tool files out of search indexing while the hosting pages remain discoverable. `sitemap.xml` lists the public webpages only.

## Update schedule

- SORCTracks historical data: monthly catch-up publication after the previous calendar month is complete
- SORCShortages: monthly through the hosted workflow, with independent health monitoring
- SORCTracks Live: daily through the hosted workflow

The hosted Live workflow generates a candidate from the current AHS public page, runs the Live parser and health checks, and commits the result only after validation. The Shortages workflow uses the documented Health Product Shortages Canada API, with the account email and password stored as the GitHub Actions secrets `SORCSHORTAGES_API_EMAIL` and `SORCSHORTAGES_API_PASSWORD`. The independent public health workflow checks SORCTracks and SORCShortages every six hours for missing data, stale outputs, fixture data, and known map errors.

The local updater remains the fallback for historical SORCTracks releases until the historical source corpus and baseline files are moved into durable hosted storage.

