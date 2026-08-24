from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "zotero_curator.py"
SPEC = importlib.util.spec_from_file_location("zotero_curator", SCRIPT)
assert SPEC and SPEC.loader
curator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = curator
SPEC.loader.exec_module(curator)

PDF_BYTES = b"%PDF-1.4\n" + b"0" * 2048


class MockConnectorHandler(BaseHTTPRequestHandler):
    server: "MockConnectorServer"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_payload(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self.server.events.append(("GET", self.path, dict(self.headers), b""))
        if self.path == "/connector/ping":
            self.send_response(200)
            self.send_header("X-Zotero-Connector-Api-Version", "3")
            self.send_header("X-Zotero-Version", "10.0")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        elif self.path == "/paper.pdf":
            self.send_payload(200, PDF_BYTES, "application/pdf")
        elif self.path == "/not-a-paper.pdf":
            self.send_payload(200, b"<html>not a PDF</html>", "text/html")
        else:
            self.send_payload(404, b"{}")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.events.append(("POST", self.path, dict(self.headers), body))
        if self.path == "/connector/getSelectedCollection":
            payload = {
                "libraryID": 1,
                "libraryName": "Test Library",
                "libraryEditable": True,
                "filesEditable": True,
                "targets": [
                    {"id": "L1", "name": "Test Library", "level": 0, "filesEditable": True},
                    {"id": "C1", "name": "Parent", "level": 1, "filesEditable": True},
                    {"id": "C2", "name": "Leaf A", "level": 2, "filesEditable": True},
                    {"id": "C3", "name": "Leaf B", "level": 1, "filesEditable": True},
                    {"id": "G9", "name": "Other Library", "level": 0, "filesEditable": True},
                    {"id": "C4", "name": "Other Leaf", "level": 1, "filesEditable": True},
                ],
            }
            self.send_payload(200, json.dumps(payload).encode("utf-8"))
        elif self.path == "/connector/saveItems":
            self.send_payload(201, b"")
        elif self.path == "/connector/updateSession":
            self.send_payload(200, b"{}")
        elif self.path == "/connector/saveAttachment":
            message = b"Library files are not editable." if self.server.attachment_status == 200 else b""
            self.send_payload(self.server.attachment_status, message)
        else:
            self.send_payload(404, b"{}")


class MockConnectorServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), MockConnectorHandler)
        self.events: list[tuple[str, str, dict[str, str], bytes]] = []
        self.attachment_status = 201


class ZoteroCuratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockConnectorServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def run_cli(self, *args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--endpoint", self.endpoint, *args],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, expected, msg=result.stdout + result.stderr)
        return result

    def write_manifest(
        self, root: Path, *, pdf_path: str = "/paper.pdf", title: str = "Mock Landmark Paper"
    ) -> Path:
        manifest = root / "manifest.tsv"
        fields = [
            "collection_id", "collection_name", "title", "year", "venue", "short_tag",
            "ccf", "pdf_spec", "source_url", "authors", "doi", "abstract",
        ]
        with manifest.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
            writer.writeheader()
            writer.writerow(
                {
                    "collection_id": "C2",
                    "collection_name": "Leaf A",
                    "title": title,
                    "year": "2024",
                    "venue": "CVPR",
                    "short_tag": "MockMethod",
                    "ccf": "CCF A",
                    "pdf_spec": f"url:{self.endpoint}{pdf_path}",
                    "source_url": "https://example.org/mock-paper",
                    "authors": "Family, Given",
                    "doi": "10.1234/mock",
                    "abstract": "A mock paper used for connector testing.",
                }
            )
        return manifest

    def test_leaf_detection_and_tag_format(self) -> None:
        selected = curator.ZoteroConnector(self.endpoint).selected_collection()
        self.assertEqual([item["id"] for item in curator.collection_targets(selected)], ["C1", "C2", "C3"])
        self.assertEqual([item["id"] for item in curator.leaf_targets(selected)], ["C2", "C3"])
        self.assertEqual(
            curator.build_tag({"year": "2024", "venue": "CVPR", "short_tag": "Method", "ccf": "CCF A"}),
            "2024-CVPR-Method-CCFA",
        )
        self.assertEqual(
            curator.build_tag({"year": "2017", "venue": "PNAS", "short_tag": "EWC", "ccf": ""}),
            "2017-PNAS-EWC",
        )

    def test_download_validate_and_import_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root)
            pdf_root = root / "Zotero_PDFs"
            state = root / "import-state.json"
            self.run_cli("validate", "--manifest", str(manifest), "--check-targets")
            self.run_cli("download", "--manifest", str(manifest), "--pdf-root", str(pdf_root))
            pdf = pdf_root / "C2_Leaf A" / "2024-MockMethod.pdf"
            self.assertTrue(curator.is_pdf(pdf))
            self.run_cli("import", "--manifest", str(manifest), "--pdf-root", str(pdf_root), "--state", str(state), "--dry-run")
            before_import = len(self.server.events)
            self.run_cli("import", "--manifest", str(manifest), "--pdf-root", str(pdf_root), "--state", str(state))
            import_events = self.server.events[before_import:]
            paths = [event[1] for event in import_events]
            self.assertEqual(paths, [
                "/connector/getSelectedCollection",
                "/connector/saveItems",
                "/connector/updateSession",
                "/connector/saveAttachment",
            ])
            saved_item = json.loads(import_events[1][3])
            update = json.loads(import_events[2][3])
            attachment_metadata = json.loads(import_events[3][2]["X-Metadata"])
            self.assertEqual(update["target"], "C2")
            self.assertEqual(update["tags"], ["2024-CVPR-MockMethod-CCFA"])
            self.assertEqual(attachment_metadata["parentItemID"], saved_item["items"][0]["id"])
            state_data = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(set(state_data), {"C2"})
            self.assertEqual(state_data["C2"]["status"], "imported")
            self.assertEqual(state_data["C2"]["tag"], "2024-CVPR-MockMethod-CCFA")
            before_retry = len(self.server.events)
            retry = self.run_cli("import", "--manifest", str(manifest), "--pdf-root", str(pdf_root), "--state", str(state))
            self.assertIn("already recorded as imported", retry.stdout)
            self.assertEqual([event[1] for event in self.server.events[before_retry:]], ["/connector/getSelectedCollection"])

    def test_rejects_html_disguised_as_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, pdf_path="/not-a-paper.pdf")
            result = self.run_cli(
                "download", "--manifest", str(manifest), "--pdf-root", str(root / "Zotero_PDFs"), expected=1
            )
            self.assertIn("did not return a PDF", result.stderr)

    def test_require_all_leaves_rejects_a_partial_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.write_manifest(Path(temporary))
            result = self.run_cli(
                "validate", "--manifest", str(manifest), "--check-targets", "--require-all-leaves", expected=2
            )
            self.assertIn("lowest-level Zotero collections missing from manifest: C3", result.stderr)

    def test_does_not_report_attachment_as_imported_when_connector_returns_200(self) -> None:
        self.server.attachment_status = 200
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root)
            pdf_root = root / "Zotero_PDFs"
            state = root / "import-state.json"
            self.run_cli("download", "--manifest", str(manifest), "--pdf-root", str(pdf_root))
            result = self.run_cli(
                "import", "--manifest", str(manifest), "--pdf-root", str(pdf_root), "--state", str(state), expected=1
            )
            self.assertIn("saveAttachment returned HTTP 200", result.stderr)
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["C2"]["status"], "attachment_failed")
            before_retry = len(self.server.events)
            self.server.attachment_status = 201
            self.run_cli("import", "--manifest", str(manifest), "--pdf-root", str(pdf_root), "--state", str(state))
            self.assertEqual(
                [event[1] for event in self.server.events[before_retry:]],
                ["/connector/getSelectedCollection", "/connector/saveAttachment"],
            )
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["C2"]["status"], "imported")

    def test_import_encodes_unicode_attachment_metadata_as_ascii_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, title="中文标题论文")
            pdf_root = root / "Zotero_PDFs"
            self.run_cli("download", "--manifest", str(manifest), "--pdf-root", str(pdf_root))
            self.run_cli("import", "--manifest", str(manifest), "--pdf-root", str(pdf_root))
            attachment = next(event for event in self.server.events if event[1] == "/connector/saveAttachment")
            self.assertTrue(attachment[2]["X-Metadata"].isascii())
            self.assertEqual(json.loads(attachment[2]["X-Metadata"])["title"], "中文标题论文")


if __name__ == "__main__":
    unittest.main()
