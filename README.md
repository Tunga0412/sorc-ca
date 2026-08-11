# SORC website package

This folder contains the deployable SORC webpages and the separately maintained tool files they embed.

## Main pages

- `index.html` — research and publications landing page
- `about.html` — SORC members and approach
- `projects.html` — ongoing projects
- `jip.html` — Junior Investigator Program
- `standards.html` — authorship and collaboration standards
- `sorctracks.html` — SORCTracks hosting page
- `methods.html` — SORCTracks methods
- `sorcshortages.html` — SORCShortages hosting page

## Tool files

- `sorctracks_tool.html` — SORCTracks interactive tool
- `sorctracks_live.html` — SORCTracks Live page with the current AHS public snapshot
- `sorcshortages_tool.html` — SORCShortages interactive tool
- `data/consolidated.js` and `data/consolidated.json` — SORCShortages tool data

The hosting pages load the tools through relative iframe paths. Keep the tool files and the `data` folder in this same directory when deploying.

`SORC_LOGO.png` supplies the favicon assets referenced by the webpages. `robots.txt` keeps the standalone tool files out of search indexing while the hosting pages remain discoverable. `sitemap.xml` lists the public webpages only.

## Update schedule

- SORCTracks: monthly, on the first of the month
- SORCShortages: monthly, on the first of the month
- SORCTracks Live: daily

For the monthly SORCShortages update, replace the dated data files and the standalone `sorcshortages_tool.html` as needed. The hosting page does not need to be edited for a routine data refresh. SORCTracks Live is maintained separately from the monthly SORCTracks release.

