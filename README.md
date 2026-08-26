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
- `data/consolidated.js` and `data/consolidated.json` - SORCShortages tool data

The hosting pages load the tools through relative iframe paths. Keep the tool files and the `data` folder in this same directory when deploying.

`SORC_LOGO.png` supplies the favicon assets referenced by the webpages. `robots.txt` keeps the standalone tool files out of search indexing while the hosting pages remain discoverable. `sitemap.xml` lists the public webpages only.

## Update schedule

- SORCTracks historical data: monthly catch-up publication after the previous calendar month is complete
- SORCShortages: maintained separately
- SORCTracks Live: daily through the hosted workflow

The hosted Live workflow generates a candidate from the current AHS public page, runs the Live parser and health checks, and commits the result only after validation. The independent public health workflow checks both tools every six hours for missing data, stale Live output, fixture data, and known map errors.

The local updater remains the fallback for historical SORCTracks releases until the historical source corpus and baseline files are moved into durable hosted storage.

