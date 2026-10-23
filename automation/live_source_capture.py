"""Private capture and bounded AHS transport for Live v8.

This module writes no public files and contains no publishing credential.
"""
from __future__ import annotations
import hashlib,json,re,os
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse
import requests

MAX_SOURCE_BYTES=8*1024*1024
MAX_TOTAL_SOURCE_BYTES=24*1024*1024
MAX_SOURCE_PAGES=20
SOURCE_HOST="www.albertahealthservices.ca"
SOURCE_PATH="/br/Page17594.aspx"

def write_private_json(directory, name, value):
    if directory is None:
        return
    root=Path(directory)
    root.mkdir(parents=True,exist_ok=True)
    target=root/name
    if target.is_symlink() or root.is_symlink():
        raise ValueError("Private receipt path must not be a symlink")
    temporary=target.with_name(target.name+".tmp")
    with temporary.open("wb") as stream:
        stream.write((json.dumps(value,sort_keys=True,ensure_ascii=False,indent=2,default=str)+"\n").encode())
        stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,target)

def default_fetch_source(url):
    with requests.get(url,headers={"User-Agent":"SORCTracks posted-notice collector/1.5"},timeout=(10,30),stream=True,allow_redirects=False) as response:
        response.raise_for_status()
        parts=[];size=0
        for part in response.iter_content(chunk_size=65536):
            size+=len(part)
            if size>MAX_SOURCE_BYTES:
                raise ValueError("AHS source exceeds the bounded response size")
            parts.append(part)
        return {"content":b"".join(parts),"final_url":response.url,"http_status":response.status_code,
                "fetched_at":datetime.now(timezone.utc).isoformat()}

class SourceCapture:
    def __init__(self,directory=None,fetch_source=None):
        if directory is None and fetch_source is None:
            raise ValueError("A private capture directory is required for a real Live collection")
        self.directory=Path(directory) if directory is not None else None
        self.injected=fetch_source is not None
        self.fetch=fetch_source or default_fetch_source
        self.pages=[];self.source_text=[];self.failures=[];self.total=0
    def get_html(self,url):
        parsed=urlparse(url)
        if parsed.scheme!="https" or parsed.hostname!=SOURCE_HOST or parsed.path!=SOURCE_PATH or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ValueError("Unexpected current AHS source authority")
        if len(self.pages)>=MAX_SOURCE_PAGES:
            raise ValueError("Current AHS source page budget exhausted")
        try:
            result=self.fetch(url)
            raw=result["content"]
            if not isinstance(raw,bytes) or len(raw)>MAX_SOURCE_BYTES:
                raise ValueError("Current AHS source bytes are invalid or oversized")
            final=urlparse(result["final_url"])
            if result["http_status"]!=200 or (final.scheme,final.hostname,final.path,final.query)!=(parsed.scheme,parsed.hostname,parsed.path,parsed.query):
                raise ValueError("AHS source status or redirect differs")
            fetched=datetime.fromisoformat(result["fetched_at"].replace("Z","+00:00"))
            if fetched.utcoffset() is None:
                raise ValueError("AHS source observation lacks an offset")
            self.total+=len(raw)
            if self.total>MAX_TOTAL_SOURCE_BYTES:
                raise ValueError("Total current AHS source response budget exhausted")
            digest=hashlib.sha256(raw).hexdigest()
            relative="source_bodies/"+digest+".html"
            if self.directory is not None:
                target=self.directory/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                if target.is_symlink():raise ValueError("Source body path is a symlink")
                if target.exists() and target.read_bytes()!=raw:raise ValueError("Source body digest collision")
                if not target.exists():
                    with target.open("xb") as stream:
                        stream.write(raw);stream.flush();os.fsync(stream.fileno())
            receipt={"requested_url":url,"final_url":result["final_url"],"http_status":200,
                     "sha256":digest,"bytes":len(raw),"fetched_at":result["fetched_at"],"body_path":relative,
                     "evidence_kind":"INJECTED_TEST_BYTES" if self.injected else "ORIGINAL_HTTP_RESPONSE_BYTES"}
            self.pages.append(receipt)
            # Source bytes are retained before any text or structural parsing.
            write_private_json(self.directory,"Source_Receipts.json",self.pages)
            text=raw.decode("utf-8-sig",errors="strict")
            if not re.search(r"<h1[^>]*>\s*AHS Facilities(?: Temporary Bed / Space Reductions|:\s*Temporary Service Disruptions)",text,re.I):
                raise ValueError("Current source lacks the AHS disruption page heading")
            self.source_text.append(text)
            return text
        except Exception as exc:
            self.failures.append({"url":url,"type":type(exc).__name__,"message":str(exc)[:300]})
            write_private_json(self.directory,"Collection_Error.json",{"status":"HOLD","failures":self.failures})
            raise
    @property
    def capture_id(self):
        payload=[{"url":x["requested_url"],"sha256":x["sha256"],"fetched_at":x["fetched_at"]} for x in self.pages]
        return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    def publisher_updated(self):
        dates=[]
        for text in self.source_text:
            match=re.search(r"Last Updated:\s*(?:</?[^>]+>\s*)*(?P<date>[A-Za-z]+\s+\d{1,2},\s+\d{4})",text,re.I)
            if match:
                try:dates.append(datetime.strptime(match.group("date"),"%B %d, %Y").date().isoformat())
                except ValueError:pass
        return max(dates) if dates else None

