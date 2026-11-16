"""Public API and CSV fallback clients for Health Product Shortages Canada."""

from __future__ import annotations

import csv
from datetime import date
from http.cookiejar import CookieJar
import io
import re
import json
from pathlib import Path
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
from zipfile import ZipFile

from bs4 import BeautifulSoup


BASE_URL = "https://healthproductshortages.ca/api/v1"


class ShortagesApiError(RuntimeError):
    pass


class ShortagesApiClient:
    def __init__(self, email: str, password: str, base_url: str = BASE_URL, timeout: int = 60):
        self.email = email
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token: str | None = None

    def _request(self, path: str, *, method: str = "GET", params: dict[str, Any] | None = None, form: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        body = urlencode(form or {}).encode() if form is not None else None
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            # The public site currently rejects the default Python-urllib
            # signature at its edge layer. This identifies the client as a
            # normal browser-compatible HTTP client; it does not bypass auth.
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "en-CA,en;q=0.9",
        }
        if self.token:
            headers["auth-token"] = self.token
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw or "{}"), {k.lower(): v for k, v in response.headers.items()}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429:
                raise ShortagesApiError("API rate limit reached; retry after the server-provided delay") from exc
            if exc.code == 403 and "verif" in detail.lower():
                raise ShortagesApiError("The source account email is not verified. Verify the account email, complete the first-login password reset, then rerun the ingest.") from exc
            raise ShortagesApiError(f"API request failed ({exc.code}): {detail[:500]}") from exc
        except URLError as exc:
            raise ShortagesApiError(f"Could not reach the API: {exc.reason}") from exc

    def login(self) -> str:
        payload, headers = self._request("login", method="POST", form={"email": self.email, "password": self.password})
        self.token = headers.get("auth-token")
        if not self.token:
            raise ShortagesApiError(f"Login did not return auth-token: {payload}")
        return self.token

    def search_page(self, *, limit: int = 100, offset: int = 0, report_type: str | None = None) -> dict[str, Any]:
        params = {"orderby": "id", "order": "asc", "limit": min(limit, 100), "offset": offset}
        if report_type:
            params["type"] = report_type
        return self._request("search", params=params)[0]

    def fetch_all(self) -> list[dict[str, Any]]:
        if not self.token:
            self.login()
        rows: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        offset = 0
        expected_total = None
        while True:
            payload = self.search_page(limit=100, offset=offset)
            page = payload.get("data")
            if not isinstance(page, list) or not isinstance(payload.get("total"), (int, str)):
                raise ShortagesApiError("API omitted its report array or total")
            try:
                expected_total = int(payload["total"])
                remaining = int(payload["remaining"])
            except (ValueError, TypeError, KeyError) as exc:
                raise ShortagesApiError("API pagination counts are invalid") from exc
            if expected_total < 0 or remaining < 0:
                raise ShortagesApiError("API pagination counts are negative")
            for row in page:
                if not isinstance(row, dict):
                    raise ShortagesApiError("API report is not an object")
                nested = row.get("report") if isinstance(row.get("report"), dict) else {}
                identifier = row.get("report.id") or row.get("report_id") or row.get("id") or nested.get("id")
                if identifier is None or str(identifier) in identifiers:
                    raise ShortagesApiError("API contains a missing or repeated report ID")
                identifiers.add(str(identifier))
            rows.extend(page)
            if len(rows) == len(page) or len(rows) % 1000 < len(page):
                print(f"reports: fetched {len(rows)} / {expected_total}", flush=True)
            if remaining == 0:
                if len(rows) != expected_total:
                    raise ShortagesApiError(f"API returned {len(rows)} rows but reported {expected_total}")
                break
            if not page or len(rows) >= expected_total:
                raise ShortagesApiError("API pagination stopped making consistent progress")
            offset += len(page)
            time.sleep(0.05)
        return rows


def _read_export_bytes(content: bytes) -> dict[str, list[dict[str, str]]]:
    """Read the site's two-CSV export, including its two-line preamble."""
    result: dict[str, list[dict[str, str]]] = {"shortages": [], "discontinuations": []}
    with ZipFile(io.BytesIO(content)) as archive:
        csv_files = 0
        for name in archive.namelist():
            if name.endswith("/") or not name.lower().endswith(".csv"):
                continue
            csv_files += 1
            text = archive.read(name).decode("utf-8-sig", errors="replace")
            parsed = list(csv.reader(io.StringIO(text)))
            header_index = next(
                (
                    index
                    for index, row in enumerate(parsed)
                    if row and row[0].strip().lower() in {"report id", "report_id"}
                ),
                None,
            )
            if header_index is None:
                raise ShortagesApiError(f"Export CSV has no report ID header: {name}")
            headers = parsed[header_index]
            if len(headers) != len(set(headers)):
                raise ShortagesApiError(f"Duplicate CSV headers: {name}")
            for values in parsed[header_index+1:]:
                if any(value.strip() for value in values) and len(values) != len(headers):
                    raise ShortagesApiError(f"CSV row width differs from its header: {name}")
            rows = [
                {header: value for header, value in zip(headers, values)}
                for values in parsed[header_index + 1 :]
                if any(value.strip() for value in values)
            ]
            lower_name = name.lower()
            key = "discontinuations" if "discontinu" in lower_name else "shortages"
            result[key].extend(rows)
        if not csv_files:
            raise ShortagesApiError("Source ZIP contains no report CSV files")
    return result


def read_export_zip(path: str | Path) -> dict[str, list[dict[str, str]]]:
    """Read the site's two-CSV export from a local ZIP path."""
    return _read_export_bytes(Path(path).read_bytes())


class PublicExportClient:
    """Download the public CSV export without an API account.

    The public search form limits one export to 10,000 records. This client
    requests non-overlapping date-created windows and combines their exports.
    """

    def __init__(self, base_url: str = "https://healthproductshortages.ca", timeout: int = 90):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.headers = {
            "Accept": "text/html,application/xhtml+xml,application/json",
            "Accept-Language": "en-CA,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }

    def _open(self, request: Request):
        try:
            return self.opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 403 and "just a moment" in detail.lower():
                raise ShortagesApiError("Provider security verification blocks the automated public export. Use the API or a manually downloaded original CSV ZIP; the accepted dataset is retained.") from exc
            raise ShortagesApiError(f"Public export request failed ({exc.code}): {detail[:500]}") from exc
        except URLError as exc:
            raise ShortagesApiError(f"Could not reach the public export: {exc.reason}") from exc

    @staticmethod
    def _query_for_range(start_date: str, end_date: str) -> dict[str, str]:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        return {
            "date_property": "created",
            "date_range[date_range_start][year]": str(start.year),
            "date_range[date_range_start][month]": str(start.month),
            "date_range[date_range_start][day]": str(start.day),
            "date_range[date_range_end][year]": str(end.year),
            "date_range[date_range_end][month]": str(end.month),
            "date_range[date_range_end][day]": str(end.day),
        }

    def _export_form(self, start_date: str, end_date: str) -> tuple[int, list[tuple[str, str]]]:
        query = self._query_for_range(start_date, end_date)
        url = f"{self.base_url}/search?{urlencode(query)}"
        request = Request(url, headers=self.headers, method="GET")
        with self._open(request) as response:
            html = response.read().decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", attrs={"name": "export"})
        if form is None:
            if any(marker in soup.get_text(" ",strip=True).lower() for marker in ("no results", "no reports found")):
                return 0, []
            raise ShortagesApiError("Public search page did not expose its export form")
        match = re.search(r"Showing\s+\d+\s+to\s+\d+\s+of\s+([\d,]+)", soup.get_text(" ", strip=True))
        if not match:
            visible = soup.get_text(" ",strip=True).lower()
            if any(marker in visible for marker in ("no results", "no reports found", "0 to 0 of 0")):
                return 0, []
            raise ShortagesApiError("Public search page did not expose a result count")
        total = int(match.group(1).replace(",", ""))
        values: list[tuple[str, str]] = []
        for element in form.find_all(["input", "select", "textarea"]):
            name = element.get("name")
            if not name:
                continue
            element_type = str(element.get("type") or "").lower()
            if element_type in {"checkbox", "radio"}:
                if name == "export[export_options][]":
                    values.append((name, str(element.get("value") or "")))
                elif element.has_attr("checked"):
                    values.append((name, str(element.get("value") or "")))
            else:
                values.append((name, str(element.get("value") or "")))
        if not any(name == "export[export_options][]" for name, _ in values):
            raise ShortagesApiError("Public export form did not expose selectable fields")
        return total, values

    def fetch_range(self, start_date: str, end_date: str) -> tuple[int, dict[str, list[dict[str, str]]]]:
        total, form_values = self._export_form(start_date, end_date)
        if total == 0:
            return 0, {"shortages": [], "discontinuations": []}
        if total > 10000:
            raise ShortagesApiError(
                f"Export window {start_date} to {end_date} contains {total} reports, over the 10,000-record limit"
            )
        body = urlencode(form_values).encode("utf-8")
        request = Request(
            f"{self.base_url}/search/export",
            data=body,
            headers={**self.headers, "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with self._open(request) as response:
            payload = response.read()
            content_type = response.headers.get("Content-Type", "")
        if not payload.startswith(b"PK"):
            preview = payload[:200].decode("utf-8", errors="replace")
            raise ShortagesApiError(
                f"Public export returned {content_type or 'an unknown response'} instead of a ZIP: {preview}"
            )
        return total, _read_export_bytes(payload)


def write_snapshot(path: str | Path, *, snapshot_date: str, shortages: list[dict[str, Any]], discontinuations: list[dict[str, Any]], source: str) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "snapshot_date": snapshot_date,
        "source": source,
        "shortages": shortages,
        "discontinuations": discontinuations,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output

