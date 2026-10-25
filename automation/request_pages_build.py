"""Request the configured branch-based Pages build without exposing credentials."""
from __future__ import annotations
import os
import hashlib
from pathlib import Path
import subprocess
import requests
REPOSITORY = "Tunga0412/sorc-ca"
def main():
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise SystemExit("Unexpected publisher repository")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GitHub Pages build token unavailable")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + token, "Accept":"application/vnd.github+json", "X-GitHub-Api-Version":"2022-11-28"})
    branch = session.get("https://api.github.com/repos/" + REPOSITORY + "/git/ref/heads/main", timeout=30)
    if branch.status_code != 200 or branch.json().get("object",{}).get("sha") != commit:
        raise SystemExit("Expected published main changed before Pages request")
    expected = (Path(__file__).resolve().parent.parent / "sorctracks_live.html").read_bytes()
    try:
        hosted = requests.get("https://sorc.ca/sorctracks_live.html", params={"pages_retry_check": commit}, headers={"Cache-Control":"no-cache"}, timeout=(15,30))
        if hosted.status_code == 200 and hashlib.sha256(hosted.content).digest() == hashlib.sha256(expected).digest():
            print("Exact accepted Live bytes are already deployed; no Pages rebuild needed")
            return
    except requests.RequestException:
        pass
    metadata = session.get("https://api.github.com/repos/" + REPOSITORY + "/pages", timeout=30)
    if metadata.status_code != 200:
        raise SystemExit("GitHub Pages configuration unavailable")
    mode = metadata.json().get("build_type")
    if mode == "workflow":
        print("Waiting for the validated Pages Actions deployment; no branch build requested")
        return
    if mode != "legacy":
        raise SystemExit("Unrecognised GitHub Pages build type")
    response = session.post("https://api.github.com/repos/" + REPOSITORY + "/pages/builds", timeout=30)
    if response.status_code != 201:
        raise SystemExit("GitHub Pages build request rejected (HTTP " + str(response.status_code) + ")")
    print("GitHub Pages build requested for accepted public main commit " + commit)
if __name__ == "__main__":
    main()

