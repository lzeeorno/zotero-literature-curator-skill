#!/usr/bin/env python3
"""Curate local Zotero collections from a reviewed paper manifest.

The command communicates only with Zotero Desktop's Connector HTTP server. It
does not open, query, or write Zotero's SQLite database.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_ENDPOINT = "http://127.0.0.1:23119"
API_VERSION = "3"
USER_AGENT = "zotero-literature-curator/1.0"
REQUIRED_COLUMNS = (
    "collection_id",
    "collection_name",
    "title",
    "year",
    "venue",
    "short_tag",
    "pdf_spec",
    "source_url",
)
PDF_MIN_BYTES = 1024


class CuratorError(RuntimeError):
    """An actionable workflow error."""


class ManifestError(CuratorError):
    """A manifest is malformed or internally inconsistent."""


class ConnectorError(CuratorError):
    """The local Zotero Connector rejected or could not serve a request."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


def direct_opener() -> urllib.request.OpenerDirector:
    """Bypass ambient proxies: localhost and public PDF hosts need direct access."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


OPEN = direct_opener()


def request(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> Response:
    merged = {"User-Agent": USER_AGENT, "Connection": "close"}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, data=body, headers=merged, method=method)
    try:
        with OPEN.open(req, timeout=timeout) as response:
            return Response(response.status, dict(response.headers.items()), response.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, dict(exc.headers.items()), exc.read())
    except urllib.error.URLError as exc:
        raise ConnectorError(f"Cannot reach {url}: {exc.reason}") from exc


class ZoteroConnector:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/")

    def call(
        self,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 60,
        method: str = "POST",
    ) -> Response:
        if payload is not None and raw is not None:
            raise ValueError("Use either payload or raw, not both")
        data = raw
        request_headers = {"Zotero-Connector-API-Version": API_VERSION}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        return request(
            self.endpoint + path,
            method=method,
            body=data,
            headers=request_headers,
            timeout=timeout,
        )

    def ping(self) -> Response:
        return self.call("/connector/ping", method="GET", timeout=8)

    def selected_collection(self) -> dict[str, Any]:
        response = self.call("/connector/getSelectedCollection", payload={})
        if response.status != 200:
            raise ConnectorError(describe_response("getSelectedCollection", response))
        try:
            data = json.loads(response.body)
        except json.JSONDecodeError as exc:
            raise ConnectorError("getSelectedCollection returned invalid JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("targets"), list):
            raise ConnectorError("getSelectedCollection response has no target collection list")
        return data


def describe_response(operation: str, response: Response) -> str:
    sample = response.body.decode("utf-8", "replace").strip().replace("\n", " ")[:500]
    return f"{operation} returned HTTP {response.status}: {sample or '<empty response>'}"


def is_collection(target: dict[str, Any]) -> bool:
    return bool(re.fullmatch(r"C\d+", str(target.get("id", ""))))


def collection_targets(selected: dict[str, Any]) -> list[dict[str, Any]]:
    all_targets = selected.get("targets", [])
    library_name = selected.get("libraryName")
    library_id = selected.get("libraryID")
    expected_personal_id = f"L{library_id}" if library_id is not None else None
    start = next(
        (
            index
            for index, target in enumerate(all_targets)
            if int(target.get("level", -1)) == 0
            and (target.get("id") == expected_personal_id or target.get("name") == library_name)
        ),
        None,
    )
    if start is None:
        raise ConnectorError("Zotero did not return a target block for the selected library")
    end = next(
        (
            index
            for index in range(start + 1, len(all_targets))
            if int(all_targets[index].get("level", -1)) == 0
        ),
        len(all_targets),
    )
    targets = [target for target in all_targets[start + 1:end] if is_collection(target)]
    if not targets:
        raise ConnectorError("Zotero returned no editable collection targets for the selected library")
    return targets


def leaf_targets(selected: dict[str, Any]) -> list[dict[str, Any]]:
    """Identify leaf collections from Zotero's depth-first, level-annotated target list."""
    targets = collection_targets(selected)
    leaves: list[dict[str, Any]] = []
    for index, target in enumerate(targets):
        level = int(target.get("level", 0))
        next_target = targets[index + 1] if index + 1 < len(targets) else None
        has_child = next_target is not None and int(next_target.get("level", 0)) > level
        if not has_child:
            leaves.append(target)
    return leaves


def validate_manifest_targets(
    rows: list[dict[str, str]], selected: dict[str, Any], *, require_all_leaves: bool
) -> tuple[int, int]:
    target_ids = {target["id"] for target in collection_targets(selected)}
    leaf_ids = {target["id"] for target in leaf_targets(selected)}
    manifest_ids = {row["collection_id"] for row in rows}
    missing = sorted(manifest_ids - target_ids)
    non_leaves = sorted(manifest_ids - leaf_ids)
    uncovered = sorted(leaf_ids - manifest_ids) if require_all_leaves else []
    messages = []
    if missing:
        messages.append("collection IDs not present in local Zotero: " + ", ".join(missing))
    if non_leaves:
        messages.append("collection IDs that are not lowest-level leaves: " + ", ".join(non_leaves))
    if uncovered:
        messages.append("lowest-level Zotero collections missing from manifest: " + ", ".join(uncovered))
    if messages:
        raise ManifestError("; ".join(messages))
    return len(manifest_ids), len(leaf_ids)


def safe_path_component(value: str, limit: int = 100) -> str:
    value = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:limit].rstrip(" .") or "untitled"


def normalize_ccf(value: str) -> str:
    normalized = re.sub(r"[\s_\-]+", "", value or "").upper()
    if normalized in {"", "NONE", "NA", "N/A"}:
        return ""
    mapping = {
        "A": "CCFA",
        "CCFA": "CCFA",
        "B": "CCFB",
        "CCFB": "CCFB",
        "C": "CCFC",
        "CCFC": "CCFC",
    }
    if normalized not in mapping:
        raise ManifestError(
            f"Unsupported CCF value {value!r}; use blank, CCFA, CCFB, or CCFC"
        )
    return mapping[normalized]


def build_tag(row: dict[str, str]) -> str:
    parts = [row["year"].strip(), row["venue"].strip(), row["short_tag"].strip()]
    if any(not part for part in parts):
        raise ManifestError("year, venue, and short_tag are required to form a tag")
    ccf = normalize_ccf(row.get("ccf", ""))
    if ccf:
        parts.append(ccf)
    return "-".join(parts)


def parse_creators(value: str) -> list[dict[str, str]]:
    """Parse `Last, First; Last, First` without fabricating creator data."""
    creators: list[dict[str, str]] = []
    for raw_creator in (value or "").split(";"):
        raw_creator = raw_creator.strip()
        if not raw_creator:
            continue
        if "," in raw_creator:
            last, first = (part.strip() for part in raw_creator.split(",", 1))
        else:
            parts = raw_creator.rsplit(" ", 1)
            first, last = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
        if last:
            creators.append({"firstName": first, "lastName": last, "creatorType": "author"})
    return creators


def arxiv_pdf_from_landing_url(url: str) -> str | None:
    match = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?)", url or "")
    return f"https://arxiv.org/pdf/{match.group(1)}" if match else None


def resolve_pdf_url(spec: str, source_url: str = "") -> str:
    spec = (spec or "").strip()
    if spec.startswith("arxiv:"):
        identifier = spec.split(":", 1)[1].strip()
        if not re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", identifier):
            raise ManifestError(f"Invalid arXiv identifier in pdf_spec: {spec!r}")
        return f"https://arxiv.org/pdf/{identifier}"
    if spec.startswith("url:"):
        url = spec.split(":", 1)[1].strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ManifestError(f"pdf_spec URL must use http or https: {spec!r}")
        return url
    if spec.startswith("search:"):
        resolved = arxiv_pdf_from_landing_url(source_url)
        if resolved:
            return resolved
        raise ManifestError(
            "search: is only supported for a reviewed manifest row whose source_url is an arXiv landing page"
        )
    raise ManifestError("pdf_spec must be `arxiv:<id>`, `url:<direct-pdf-url>`, or a reviewed `search:` row")


def pdf_path(row: dict[str, str], pdf_root: Path) -> Path:
    directory = pdf_root / f"{row['collection_id']}_{safe_path_component(row['collection_name'])}"
    filename = f"{row['year']}-{safe_path_component(row['short_tag'])}.pdf"
    return directory / filename


def is_pdf(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= PDF_MIN_BYTES and path.read_bytes()[:5] == b"%PDF-"
    except OSError:
        return False


def read_manifest(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ManifestError(f"Manifest does not exist: {path}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames is None:
            raise ManifestError("Manifest must be a tab-separated file with a header row")
        missing = [column for column in REQUIRED_COLUMNS if column not in reader.fieldnames]
        if missing:
            raise ManifestError("Manifest is missing required columns: " + ", ".join(missing))
        rows = []
        for line_number, row in enumerate(reader, start=2):
            normalized = {key: (value or "").strip() for key, value in row.items() if key}
            normalized["_line"] = str(line_number)
            rows.append(normalized)
    if not rows:
        raise ManifestError("Manifest has no paper rows")
    errors = validate_manifest_rows(rows)
    if errors:
        raise ManifestError("Manifest validation failed:\n- " + "\n- ".join(errors))
    return rows


def validate_manifest_rows(rows: list[dict[str, str]]) -> list[str]:
    errors: list[str] = []
    seen_collections: set[str] = set()
    seen_tags: set[str] = set()
    for row in rows:
        line = row.get("_line", "?")
        for column in REQUIRED_COLUMNS:
            if not row.get(column, "").strip():
                errors.append(f"line {line}: {column} is empty")
        collection_id = row.get("collection_id", "")
        if collection_id and not re.fullmatch(r"C\d+", collection_id):
            errors.append(f"line {line}: collection_id must look like C123, got {collection_id!r}")
        if collection_id in seen_collections:
            errors.append(f"line {line}: duplicate collection_id {collection_id}")
        seen_collections.add(collection_id)
        year = row.get("year", "")
        if year and not re.fullmatch(r"\d{4}", year):
            errors.append(f"line {line}: year must be four digits, got {year!r}")
        try:
            resolve_pdf_url(row.get("pdf_spec", ""), row.get("source_url", ""))
            tag = build_tag(row)
            if tag in seen_tags:
                errors.append(f"line {line}: duplicate tag {tag}")
            seen_tags.add(tag)
        except ManifestError as exc:
            errors.append(f"line {line}: {exc}")
    return errors


def write_tsv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CuratorError(f"Cannot read import state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CuratorError(f"Import state {path} must be a JSON object")
    return value


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".zotero-curator-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        Path(temporary_name).replace(path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def download_pdf(url: str, destination: Path) -> int:
    response = request(url, headers={"Accept": "application/pdf"}, timeout=180)
    if response.status != 200:
        raise CuratorError(describe_response(f"download {url}", response))
    if not response.body.startswith(b"%PDF-"):
        content_type = response.headers.get("Content-Type", "unknown")
        prefix = response.body[:80].decode("utf-8", "replace")
        raise CuratorError(f"{url} did not return a PDF (content-type={content_type}, prefix={prefix!r})")
    if len(response.body) < PDF_MIN_BYTES:
        raise CuratorError(f"{url} returned an implausibly small PDF ({len(response.body)} bytes)")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(response.body)
    return len(response.body)


def connector_item(row: dict[str, str], connector_id: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": connector_id,
        "itemType": row.get("item_type") or "journalArticle",
        "title": row["title"],
        "creators": parse_creators(row.get("authors", "")),
        "abstractNote": row.get("abstract", ""),
        "publicationTitle": row["venue"],
        "date": row["year"],
        "url": row["source_url"],
        "language": row.get("language") or "en",
    }
    if row.get("doi"):
        item["DOI"] = row["doi"]
    return item


def create_item(
    connector: ZoteroConnector, row: dict[str, str], session_id: str, connector_id: str
) -> Response:
    item = connector_item(row, connector_id)
    response = connector.call(
        "/connector/saveItems",
        payload={"sessionID": session_id, "uri": row["source_url"], "items": [item]},
    )
    if response.status not in {200, 201}:
        raise ConnectorError(describe_response("saveItems", response))
    return response


def update_item_collection(
    connector: ZoteroConnector, row: dict[str, str], session_id: str
) -> Response:
    response = connector.call(
        "/connector/updateSession",
        payload={"sessionID": session_id, "target": row["collection_id"], "tags": [build_tag(row)], "note": ""},
    )
    if response.status != 200:
        raise ConnectorError(describe_response("updateSession", response))
    return response


def attach_pdf(
    connector: ZoteroConnector,
    row: dict[str, str],
    attachment_path: Path,
    session_id: str,
    connector_id: str,
) -> Response:
    metadata = json.dumps(
        {
            "sessionID": session_id,
            "parentItemID": connector_id,
            "title": row["title"],
            "url": resolve_pdf_url(row["pdf_spec"], row["source_url"]),
        },
        # HTTP headers are latin-1 in urllib/http.client. Escaped JSON preserves
        # Chinese titles while keeping X-Metadata valid ASCII.
        ensure_ascii=True,
    )
    response = connector.call(
        "/connector/saveAttachment",
        raw=attachment_path.read_bytes(),
        headers={"Content-Type": "application/pdf", "X-Metadata": metadata},
        timeout=300,
    )
    return response


def initial_state(row: dict[str, str], attachment: Path) -> dict[str, Any]:
    return {
        "status": "creating_item",
        "title": row["title"],
        "tag": build_tag(row),
        "pdf": str(attachment),
        "source_url": row["source_url"],
        "pdf_url": resolve_pdf_url(row["pdf_spec"], row["source_url"]),
        "session_id": f"zotero-curator-{uuid.uuid4().hex}",
        "connector_id": f"paper-{row['collection_id']}-{uuid.uuid4().hex}",
    }


def cmd_status(args: argparse.Namespace) -> int:
    connector = ZoteroConnector(args.endpoint)
    ping = connector.ping()
    if ping.status != 200:
        raise ConnectorError(describe_response("ping", ping))
    selected = connector.selected_collection()
    api_version = ping.headers.get("X-Zotero-Connector-API-Version", "unknown")
    version = ping.headers.get("X-Zotero-Version", "unknown")
    print(f"connector=reachable api={api_version} zotero={version}")
    print(
        "library={library} editable={editable} files_editable={files}".format(
            library=selected.get("libraryName", "unknown"),
            editable=selected.get("libraryEditable", False),
            files=selected.get("filesEditable", False),
        )
    )
    print(f"collections={len(collection_targets(selected))} leaves={len(leaf_targets(selected))}")
    return 0


def cmd_inventory(args: argparse.Namespace) -> int:
    selected = ZoteroConnector(args.endpoint).selected_collection()
    targets = leaf_targets(selected) if args.leaves_only else collection_targets(selected)
    rows = [
        {
            "collection_id": target["id"],
            "level": target.get("level", ""),
            "collection_name": target.get("name", ""),
        }
        for target in targets
    ]
    if args.out:
        write_tsv(Path(args.out), rows, ["collection_id", "level", "collection_name"])
        print(f"wrote={args.out} collections={len(rows)}")
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=["collection_id", "level", "collection_name"], delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    rows = read_manifest(manifest)
    print(f"manifest=valid rows={len(rows)}")
    if args.require_all_leaves and not args.check_targets:
        raise ManifestError("--require-all-leaves requires --check-targets")
    if args.check_targets:
        selected = ZoteroConnector(args.endpoint).selected_collection()
        matched, leaves = validate_manifest_targets(
            rows, selected, require_all_leaves=args.require_all_leaves
        )
        print(f"zotero_targets=valid manifest_collections={matched} leaves={leaves}")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    rows = read_manifest(Path(args.manifest))
    root = Path(args.pdf_root)
    failed = 0
    for row in rows:
        destination = pdf_path(row, root)
        if is_pdf(destination) and not args.overwrite:
            print(f"SKIP {row['collection_id']} {destination}")
            continue
        try:
            source = resolve_pdf_url(row["pdf_spec"], row["source_url"])
            size = download_pdf(source, destination)
            print(f"DOWNLOADED {row['collection_id']} bytes={size} source={source}")
        except CuratorError as exc:
            failed += 1
            print(f"FAILED {row['collection_id']}: {exc}", file=sys.stderr)
    print(f"downloaded_or_existing={len(rows) - failed} failed={failed}")
    return 1 if failed else 0


def cmd_import(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    rows = read_manifest(manifest_path)
    root = Path(args.pdf_root)
    connector = ZoteroConnector(args.endpoint)
    selected = connector.selected_collection()
    validate_manifest_targets(rows, selected, require_all_leaves=args.require_all_leaves)
    if not selected.get("libraryEditable") or not selected.get("filesEditable"):
        raise ConnectorError("The selected Zotero library is not editable for items and files")
    missing_pdfs = [str(pdf_path(row, root)) for row in rows if not is_pdf(pdf_path(row, root))]
    if missing_pdfs:
        raise CuratorError("Missing or invalid local PDF(s):\n- " + "\n- ".join(missing_pdfs))
    if args.dry_run:
        for row in rows:
            print(f"WOULD IMPORT {row['collection_id']} tag={build_tag(row)} pdf={pdf_path(row, root)}")
        print(f"dry_run rows={len(rows)}")
        return 0

    state_path = Path(args.state) if args.state else manifest_path.with_name(manifest_path.stem + ".zotero-state.json")
    state = load_state(state_path)
    failed = 0
    for row in rows:
        collection_id = row["collection_id"]
        attachment = pdf_path(row, root)
        record = state.get(collection_id)
        if record is None:
            record = initial_state(row, attachment)
            state[collection_id] = record
            save_state(state_path, state)
            try:
                create_item(connector, row, record["session_id"], record["connector_id"])
                record["status"] = "item_created"
                save_state(state_path, state)
            except (CuratorError, OSError) as exc:
                failed += 1
                record["error"] = str(exc)
                save_state(state_path, state)
                print(f"FAILED {collection_id}: {exc}", file=sys.stderr)
                continue
        if record.get("status") == "imported":
            print(f"SKIP {collection_id} already recorded as imported")
            continue
        if record.get("status") in {"creating_item", "attaching_pdf"}:
            failed += 1
            print(
                f"SKIP {collection_id}: a prior {record['status']} request has an uncertain outcome; inspect Zotero before retrying",
                file=sys.stderr,
            )
            continue
        if record.get("status") not in {"item_created", "collection_updated", "attachment_failed"}:
            failed += 1
            print(f"SKIP {collection_id}: unsupported import state {record.get('status')!r}", file=sys.stderr)
            continue
        try:
            if record["status"] == "item_created":
                update_item_collection(connector, row, record["session_id"])
                record["status"] = "collection_updated"
                record.pop("error", None)
                save_state(state_path, state)
            if record["status"] in {"collection_updated", "attachment_failed"}:
                record["status"] = "attaching_pdf"
                record.pop("error", None)
                save_state(state_path, state)
                response = attach_pdf(
                    connector, row, attachment, record["session_id"], record["connector_id"]
                )
                if response.status != 201:
                    record["error"] = describe_response("saveAttachment", response)
                    # Zotero's documented 200 response proves no file was added, so
                    # a later run may safely retry after file permissions are fixed.
                    if response.status == 200:
                        record["status"] = "attachment_failed"
                    save_state(state_path, state)
                    raise ConnectorError(record["error"])
                record["status"] = "imported"
                record["imported_at"] = int(time.time())
                save_state(state_path, state)
                print(f"IMPORTED {collection_id} tag={record['tag']}")
        except (CuratorError, OSError) as exc:
            failed += 1
            record["error"] = str(exc)
            save_state(state_path, state)
            print(f"FAILED {collection_id}: {exc}", file=sys.stderr)
    print(f"processed={len(rows)} failed={failed} state={state_path}")
    return 1 if failed else 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help=f"Zotero Connector endpoint (default: {DEFAULT_ENDPOINT})")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="check the local Zotero Connector without changing Zotero")

    inventory = commands.add_parser("inventory", help="export local Zotero collection targets")
    inventory.add_argument("--leaves-only", action="store_true", help="export only lowest-level collections")
    inventory.add_argument("--out", help="write TSV to this path instead of stdout")

    validate = commands.add_parser("validate", help="validate a reviewed paper manifest")
    validate.add_argument("--manifest", required=True)
    validate.add_argument("--check-targets", action="store_true", help="also check IDs against local Zotero")
    validate.add_argument(
        "--require-all-leaves",
        action="store_true",
        help="require exactly one row for every current lowest-level Zotero collection",
    )

    download = commands.add_parser("download", help="download and validate local PDF copies from the manifest")
    download.add_argument("--manifest", required=True)
    download.add_argument("--pdf-root", required=True)
    download.add_argument("--overwrite", action="store_true")

    importer = commands.add_parser("import", help="create Zotero items, tags, and managed local PDF attachments")
    importer.add_argument("--manifest", required=True)
    importer.add_argument("--pdf-root", required=True)
    importer.add_argument("--state", help="local resumable import-state JSON path")
    importer.add_argument("--dry-run", action="store_true", help="check all local inputs without modifying Zotero")
    importer.add_argument(
        "--require-all-leaves",
        action="store_true",
        help="require exactly one row for every current lowest-level Zotero collection",
    )
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return {
            "status": cmd_status,
            "inventory": cmd_inventory,
            "validate": cmd_validate,
            "download": cmd_download,
            "import": cmd_import,
        }[args.command](args)
    except CuratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
