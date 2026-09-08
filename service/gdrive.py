"""Pull publicly-shared Google Drive files down to disk, for vault upload.

Scope is deliberately narrow: **anyone-with-the-link** files and folders, read
with a plain API key. No OAuth, no consent screen, no refresh tokens — the
operator pastes a link, we fetch the bytes. (Contrast automations/push_to_sheets,
which needs real OAuth because it writes to a private spreadsheet.)

Why an API key at all, when a public file has a plain download URL? Because the
`drive.google.com/uc?export=download` route serves an HTML interstitial for
anything big enough to warrant a virus-scan warning, and parsing your way past
that is a guessing game. `files.get?alt=media` on the API host just returns the
bytes, at any size, and gives us `size`/`mimeType` up front so a batch can be
rejected before a gigabyte moves.

Get a key at console.cloud.google.com → APIs & Services → Credentials → API key,
with the Drive API enabled on the project. Restrict it to the Drive API. Store it
in Setup → Keys (`GOOGLE_DRIVE_API_KEY`) or the environment.

WHAT THIS CANNOT DO, and how it fails:
  * Private files. With key-only auth Drive answers **404, not 403**, for a file
    that exists but isn't shared — indistinguishable from a bad id. The error
    text says both, because we genuinely cannot tell them apart.
  * Files whose owner disabled downloading, and abuse-flagged files: 403 with a
    `cannotDownloadFile` / `cannotDownloadAbusiveFile` reason. Surfaced as-is.
  * Shared-drive items. Listing passes `supportsAllDrives`, but items on a shared
    drive are rarely link-public, so expect empty results rather than an error.
  * Google-native docs (Docs/Sheets/Slides). They have no bytes to download and
    are skipped by mime type, not silently failed.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("of-relay.gdrive")

API = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
_NATIVE_PREFIX = "application/vnd.google-apps."

# Drive hands back HTML on some error paths; a media response that starts with
# "<" is never a real image/video and would otherwise land on disk as a
# corrupt file that only fails much later, at the S3 PUT.
_HTML_SNIFF = b"<"


class GDriveError(RuntimeError):
    """Anything the operator needs to read and act on."""


@dataclass(frozen=True)
class DriveRef:
    """A parsed Drive link. `kind` is 'file' | 'folder' | 'unknown'.

    'unknown' is honest rather than lazy: `/open?id=` and `uc?id=` name an id
    without saying what it is, and only a metadata call can tell us.
    """
    id: str
    kind: str
    resource_key: str | None = None


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int | None
    resource_key: str | None = None
    # Google's own md5 of the content. Free in the listing, and the key that
    # lets an importer skip a file it already has without spending a download —
    # which matters because Drive rate-limits hard (it starts serving an HTML
    # "Sorry..." page) long before you notice you are wasting bandwidth.
    md5: str | None = None


def api_key() -> str:
    """The Drive key: secrets store first (Setup UI), then the environment."""
    try:
        import secrets_store
        if k := secrets_store.get("GOOGLE_DRIVE_API_KEY"):
            return k.strip()
    except Exception:
        log.debug("secrets_store unavailable; falling back to env", exc_info=True)
    if k := (os.environ.get("GOOGLE_DRIVE_API_KEY") or "").strip():
        return k
    raise GDriveError(
        "No GOOGLE_DRIVE_API_KEY configured. Add one in Setup → Keys, or set it "
        "in the relay environment. Create the key at console.cloud.google.com → "
        "APIs & Services → Credentials, with the Drive API enabled."
    )


def parse_link(link: str) -> DriveRef:
    """Turn any Drive URL (or a bare id) into a DriveRef.

    `resourcekey` matters: files shared before Google's 2021 link-security
    change are unreachable by id alone and need the key echoed back in a
    header. Dropping it turns a working link into a confusing 404.
    """
    s = (link or "").strip()
    if not s:
        raise GDriveError("empty Google Drive link")

    # A bare id — no slashes, no scheme, Drive's id alphabet.
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", s):
        return DriveRef(id=s, kind="unknown")

    u = urlparse(s if "://" in s else f"https://{s}")
    if "drive.google.com" not in u.netloc and "docs.google.com" not in u.netloc:
        raise GDriveError(f"not a Google Drive link: {link}")

    q = parse_qs(u.query)
    rkey = (q.get("resourcekey") or [None])[0]

    if m := re.search(r"/folders/([A-Za-z0-9_-]+)", u.path):
        return DriveRef(id=m.group(1), kind="folder", resource_key=rkey)
    if m := re.search(r"/file/d/([A-Za-z0-9_-]+)", u.path):
        return DriveRef(id=m.group(1), kind="file", resource_key=rkey)
    # /document/d/, /spreadsheets/d/ … — native docs, resolved then rejected.
    if m := re.search(r"/d/([A-Za-z0-9_-]+)", u.path):
        return DriveRef(id=m.group(1), kind="unknown", resource_key=rkey)
    if fid := (q.get("id") or [None])[0]:
        return DriveRef(id=fid, kind="unknown", resource_key=rkey)

    raise GDriveError(f"could not find a file or folder id in: {link}")


def _headers(*refs) -> dict:
    """`X-Goog-Drive-Resource-Keys: <id>/<key>,…` for every ref that has one."""
    pairs = [f"{r.id}/{r.resource_key}" for r in refs
             if r is not None and getattr(r, "resource_key", None)]
    return {"X-Goog-Drive-Resource-Keys": ",".join(pairs)} if pairs else {}


def _explain(status: int, body: str, what: str) -> GDriveError:
    low = (body or "").lower()
    if "<title>sorry" in low or "unusual traffic" in low:
        return GDriveError(
            f"{what}: Google is rate-limiting this key or IP (it served its "
            f"\"Sorry…\" page, not an API error). Wait a few minutes; avoid "
            f"re-downloading files you already have.")
    if status == 404:
        return GDriveError(
            f"{what}: not found, or not shared publicly. With an API key these "
            f"are indistinguishable — open the link in a private window to "
            f"check that 'Anyone with the link' is set.")
    if status == 403:
        if "abusive" in low:
            return GDriveError(f"{what}: Drive flagged this file as abusive and "
                               f"refuses to serve it.")
        if "cannotdownload" in low.replace("_", ""):
            return GDriveError(f"{what}: the owner disabled downloading for this file.")
        if "quota" in low or "ratelimit" in low:
            return GDriveError(f"{what}: Drive API quota exceeded — retry later, "
                               f"or raise the quota on the key's project.")
        return GDriveError(f"{what}: access denied by Drive ({body[:200]})")
    if status == 400 and "api key not valid" in low:
        return GDriveError(f"{what}: the GOOGLE_DRIVE_API_KEY is not valid, or the "
                           f"Drive API is not enabled on its project.")
    return GDriveError(f"{what}: Drive returned {status} — {body[:200]}")


def _session():
    import requests
    return requests


def metadata(ref: DriveRef) -> DriveFile:
    """One `files.get` — resolves what the id actually is."""
    r = _session().get(
        f"{API}/{ref.id}",
        params={"fields": "id,name,mimeType,size,md5Checksum,shortcutDetails",
                "supportsAllDrives": "true", "key": api_key()},
        headers=_headers(ref), timeout=30)
    if not r.ok:
        raise _explain(r.status_code, r.text, f"Drive item {ref.id}")
    d = r.json()
    # A shortcut is a pointer; follow it once so a folder of shortcuts works.
    if d.get("mimeType") == SHORTCUT_MIME:
        target = (d.get("shortcutDetails") or {}).get("targetId")
        if not target:
            raise GDriveError(f"{d.get('name')}: shortcut with no target")
        return metadata(DriveRef(id=target, kind="unknown",
                                 resource_key=ref.resource_key))
    return DriveFile(id=d["id"], name=d.get("name") or d["id"],
                     mime_type=d.get("mimeType") or "application/octet-stream",
                     size=int(d["size"]) if d.get("size") else None,
                     resource_key=ref.resource_key, md5=d.get("md5Checksum"))


def list_folder(ref: DriveRef, *, recursive: bool = True,
                max_files: int = 500, _depth: int = 0) -> list[DriveFile]:
    """Every downloadable file in a public folder.

    Paginates properly — `fields=files(...)` alone silently omits
    `nextPageToken`, which caps you at one page and looks like a small folder.
    Recurses into subfolders, depth-capped so a cyclic shortcut graph can't
    spin forever.
    """
    if _depth > 8:
        log.warning("gdrive: folder nesting deeper than 8, stopping at %s", ref.id)
        return []
    out: list[DriveFile] = []
    token = None
    while True:
        params = {
            "q": f"'{ref.id}' in parents and trashed=false",
            "fields": "nextPageToken, files(id,name,mimeType,size,md5Checksum,shortcutDetails)",
            "pageSize": "200",
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
            "key": api_key(),
        }
        if token:
            params["pageToken"] = token
        r = _session().get(API, params=params, headers=_headers(ref), timeout=30)
        if not r.ok:
            raise _explain(r.status_code, r.text, f"Drive folder {ref.id}")
        page = r.json()
        for d in page.get("files") or []:
            mime = d.get("mimeType") or ""
            if mime == FOLDER_MIME:
                if recursive:
                    out.extend(list_folder(DriveRef(id=d["id"], kind="folder",
                                                    resource_key=ref.resource_key),
                                           recursive=True, max_files=max_files,
                                           _depth=_depth + 1))
                continue
            if mime == SHORTCUT_MIME:
                try:
                    out.append(metadata(DriveRef(id=d["id"], kind="unknown",
                                                 resource_key=ref.resource_key)))
                except GDriveError as e:
                    log.warning("gdrive: skipping shortcut %s — %s", d.get("name"), e)
                continue
            out.append(DriveFile(id=d["id"], name=d.get("name") or d["id"],
                                 mime_type=mime,
                                 size=int(d["size"]) if d.get("size") else None,
                                 resource_key=ref.resource_key,
                                 md5=d.get("md5Checksum")))
            if len(out) >= max_files:
                log.warning("gdrive: folder %s hit the %d-file cap", ref.id, max_files)
                return out
        token = page.get("nextPageToken")
        if not token:
            return out


def is_uploadable(f: DriveFile) -> tuple[bool, str]:
    """Can this become vault media? Returns (ok, reason-if-not)."""
    if f.mime_type.startswith(_NATIVE_PREFIX):
        return False, f"Google-native file ({f.mime_type.rsplit('.', 1)[-1]}) has no media to upload"
    if not f.mime_type.startswith(("image/", "video/", "audio/")):
        return False, f"unsupported type {f.mime_type}"
    return True, ""


def resolve(links: list[str], *, max_files: int = 500) -> list[DriveFile]:
    """Expand a mixed list of file and folder links into concrete files.

    De-duplicates by Drive id, so pasting a folder and one of its files (or the
    same link twice) doesn't upload anything twice.
    """
    seen: set[str] = set()
    out: list[DriveFile] = []
    for link in links:
        ref = parse_link(link)
        if ref.kind == "folder":
            files = list_folder(ref, max_files=max_files)
        else:
            meta = metadata(ref)
            files = (list_folder(DriveRef(id=meta.id, kind="folder",
                                          resource_key=ref.resource_key),
                                 max_files=max_files)
                     if meta.mime_type == FOLDER_MIME else [meta])
        for f in files:
            if f.id not in seen:
                seen.add(f.id)
                out.append(f)
    return out


def download(f: DriveFile, dest_dir: str | Path, *,
             max_bytes: int | None = None) -> Path:
    """Stream one Drive file to `dest_dir`. Returns the written path.

    Enforces `max_bytes` **while streaming**, not just against the declared
    metadata size — Drive's `size` is absent for some items and a caller that
    trusted it alone could still be handed a gigabyte.
    """
    if max_bytes is not None and f.size is not None and f.size > max_bytes:
        raise GDriveError(
            f"{f.name}: {f.size / 1e6:.0f} MB exceeds the {max_bytes / 1e6:.0f} MB limit")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Drive names are user-supplied: keep the basename only, so a name like
    # "../../etc/x" cannot escape the spool directory.
    safe = re.sub(r"[^\w.\- ]+", "_", Path(f.name).name).strip() or f.id
    dest = dest_dir / f"{f.id}_{safe}"

    r = _session().get(f"{API}/{f.id}",
                       params={"alt": "media", "supportsAllDrives": "true",
                               "key": api_key()},
                       headers=_headers(f), stream=True, timeout=120)
    if not r.ok:
        raise _explain(r.status_code, r.text, f"downloading {f.name}")

    written = 0
    try:
        with dest.open("wb") as fh:
            for chunk in r.iter_content(1024 * 256):
                if not chunk:
                    continue
                if not written and chunk.lstrip()[:1] == _HTML_SNIFF:
                    raise GDriveError(
                        f"{f.name}: Drive served an HTML page instead of the file "
                        f"— it is probably not publicly shared.")
                written += len(chunk)
                if max_bytes is not None and written > max_bytes:
                    raise GDriveError(
                        f"{f.name}: exceeded the {max_bytes / 1e6:.0f} MB limit mid-download")
                fh.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)   # never leave a partial file for the uploader
        raise
    log.info("gdrive: downloaded %s (%d bytes) -> %s", f.name, written, dest.name)
    return dest
