"""
Tests for the SharePoint connector's Use Case 2 surface: the generalised
list_new_files/fetch_folder (extension filtering + move-after-download),
ops.intake.gather_from_sharepoint, and the write-back calls in
ops/filer.py and ops/audit_pack.py. Use Case 1's existing behaviour
(list_new_pdfs, fetch_submission_folder) is covered too, since both are
now thin wrappers over the generalised functions and must keep their
original behaviour unchanged.

All Graph API calls are mocked at the requests boundary (or, for the
higher-level ops.intake/ops.filer/ops.audit_pack tests, at the
sharepoint_client function boundary) - nothing here makes a real network
call.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.connectors import sharepoint_client
from config.settings import settings
from ops.audit_pack import compile_pack, evaluate_pack
from ops.correlator import correlate
from ops.models import OpsDocument


def _doc(name, doc_type="TRADE_ORDER", **meta) -> OpsDocument:
    d = OpsDocument(source_file=name, source_path=f"/tmp/{name}", doc_type_code=doc_type,
                    doc_type_name=doc_type.replace("_", " ").title())
    d.metadata.update({k: str(v) for k, v in meta.items()})
    return d


def _configure_sharepoint(monkeypatch):
    monkeypatch.setattr(settings.sharepoint, "tenant_id", "tenant")
    monkeypatch.setattr(settings.sharepoint, "client_id", "client")
    monkeypatch.setattr(settings.sharepoint, "client_secret", "secret")
    monkeypatch.setattr(settings.sharepoint, "site_id", "site")
    monkeypatch.setattr(sharepoint_client, "_get_access_token", lambda: "fake-token")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# --------------------------------------------------- list_new_files

def test_list_new_files_filters_by_extension_case_insensitive(monkeypatch):
    _configure_sharepoint(monkeypatch)
    payload = {"value": [
        {"id": "1", "name": "instruction.PDF", "file": {}, "size": 10},
        {"id": "2", "name": "note.eml", "file": {}, "size": 5},
        {"id": "3", "name": "readme.txt", "file": {}, "size": 3},
        {"id": "4", "name": "spreadsheet.xlsx", "file": {}, "size": 20},
        {"id": "5", "name": "a folder", "size": 0},  # no "file" key -> not a file
    ]}
    monkeypatch.setattr(sharepoint_client.requests, "get", lambda *a, **k: _FakeResponse(payload))

    items = sharepoint_client.list_new_files("Ops Instructions/Submissions", (".pdf", ".eml", ".txt"))

    assert {i["name"] for i in items} == {"instruction.PDF", "note.eml", "readme.txt"}


def test_list_new_files_follows_pagination(monkeypatch):
    _configure_sharepoint(monkeypatch)
    page1 = {"value": [{"id": "1", "name": "a.pdf", "file": {}}], "@odata.nextLink": "https://next"}
    page2 = {"value": [{"id": "2", "name": "b.pdf", "file": {}}]}
    responses = [_FakeResponse(page1), _FakeResponse(page2)]
    monkeypatch.setattr(sharepoint_client.requests, "get", lambda *a, **k: responses.pop(0))

    items = sharepoint_client.list_new_files("some/folder", (".pdf",))

    assert {i["name"] for i in items} == {"a.pdf", "b.pdf"}


def test_list_new_pdfs_defaults_to_configured_ut_folder_and_pdf_only(monkeypatch):
    # UT's original call site - must keep behaving exactly as before
    # list_new_files was generalised.
    monkeypatch.setattr(settings.sharepoint, "submission_folder", "UT Instructions/Submissions")
    calls = []
    monkeypatch.setattr(sharepoint_client, "list_new_files",
                         lambda folder_path, extensions=(".pdf",): calls.append((folder_path, extensions)) or [])

    sharepoint_client.list_new_pdfs()

    assert calls == [("UT Instructions/Submissions", (".pdf",))]


# --------------------------------------------------- fetch_folder

def test_fetch_folder_returns_empty_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings.sharepoint, "tenant_id", "")
    calls = []
    monkeypatch.setattr(sharepoint_client, "list_new_files", lambda *a, **k: calls.append("called") or [])

    result = sharepoint_client.fetch_folder(Path("/tmp/dest"), "some/folder")

    assert result == []
    assert calls == []


def test_fetch_folder_leaves_source_in_place_when_move_processed_to_is_none(monkeypatch, tmp_path):
    _configure_sharepoint(monkeypatch)
    items = [{"id": "1", "name": "a.pdf"}, {"id": "2", "name": "b.pdf"}]
    monkeypatch.setattr(sharepoint_client, "list_new_files", lambda *a, **k: items)
    monkeypatch.setattr(sharepoint_client, "download_file", lambda item, dest_dir: dest_dir / item["name"])
    moved = []
    monkeypatch.setattr(sharepoint_client, "delete_or_move_source", lambda *a, **k: moved.append(a) or True)

    downloaded = sharepoint_client.fetch_folder(tmp_path, "some/folder", (".pdf",))

    assert len(downloaded) == 2
    assert moved == []


def test_fetch_folder_moves_each_item_when_move_processed_to_given(monkeypatch, tmp_path):
    _configure_sharepoint(monkeypatch)
    items = [{"id": "1", "name": "a.pdf"}, {"id": "2", "name": "b.pdf"}]
    monkeypatch.setattr(sharepoint_client, "list_new_files", lambda *a, **k: items)
    monkeypatch.setattr(sharepoint_client, "download_file", lambda item, dest_dir: dest_dir / item["name"])
    moved = []
    monkeypatch.setattr(sharepoint_client, "delete_or_move_source",
                         lambda item_id, target: moved.append((item_id, target)) or True)

    sharepoint_client.fetch_folder(
        tmp_path, "Ops Instructions/Submissions", (".pdf",),
        move_processed_to="Ops Instructions/Submissions/Processed",
    )

    assert moved == [
        ("1", "Ops Instructions/Submissions/Processed"),
        ("2", "Ops Instructions/Submissions/Processed"),
    ]


def test_fetch_submission_folder_keeps_ut_original_no_move_behaviour(monkeypatch, tmp_path):
    # UT's original call site never passed move_processed_to - must stay that way.
    _configure_sharepoint(monkeypatch)
    calls = []

    def fake_fetch_folder(dest_dir, folder_path, extensions=(".pdf",), move_processed_to=None):
        calls.append((folder_path, extensions, move_processed_to))
        return []

    monkeypatch.setattr(settings.sharepoint, "submission_folder", "UT Instructions/Submissions")
    monkeypatch.setattr(sharepoint_client, "fetch_folder", fake_fetch_folder)

    sharepoint_client.fetch_submission_folder(tmp_path)

    assert calls == [("UT Instructions/Submissions", (".pdf",), None)]


# --------------------------------------------------- ops.intake.gather_from_sharepoint

def test_gather_from_sharepoint_returns_empty_when_not_configured(monkeypatch):
    import ops.intake as intake_mod
    monkeypatch.setattr(sharepoint_client, "is_configured", lambda: False)

    assert intake_mod.gather_from_sharepoint() == []


def test_gather_from_sharepoint_downloads_and_labels_items_source_sharepoint(monkeypatch, tmp_path):
    import ops.intake as intake_mod
    monkeypatch.setattr(intake_mod, "OPS_INTAKE_DIR", tmp_path)
    monkeypatch.setattr(intake_mod, "ensure_output_dirs", lambda: None)
    monkeypatch.setattr(sharepoint_client, "is_configured", lambda: True)

    def fake_fetch_folder(dest_dir, folder_path, extensions=(".pdf",), move_processed_to=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = dest_dir / "instr.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test")
        return [pdf_path]

    monkeypatch.setattr(sharepoint_client, "fetch_folder", fake_fetch_folder)

    items = intake_mod.gather_from_sharepoint()

    assert len(items) == 1
    assert items[0].source_label == "SharePoint"
    assert items[0].kind == "pdf"


def test_gather_from_sharepoint_passes_configured_folder_and_move_setting(monkeypatch, tmp_path):
    import ops.intake as intake_mod
    monkeypatch.setattr(intake_mod, "OPS_INTAKE_DIR", tmp_path)
    monkeypatch.setattr(intake_mod, "ensure_output_dirs", lambda: None)
    monkeypatch.setattr(sharepoint_client, "is_configured", lambda: True)
    monkeypatch.setattr(settings.sharepoint, "ops_submission_folder", "Ops Instructions/Submissions")
    monkeypatch.setattr(settings.sharepoint, "ops_processed_folder", "Ops Instructions/Submissions/Processed")
    monkeypatch.setattr(settings.sharepoint, "ops_mark_processed", True)

    calls = []

    def fake_fetch_folder(dest_dir, folder_path, extensions=(".pdf",), move_processed_to=None):
        calls.append((folder_path, extensions, move_processed_to))
        return []

    monkeypatch.setattr(sharepoint_client, "fetch_folder", fake_fetch_folder)

    intake_mod.gather_from_sharepoint()

    assert calls == [(
        "Ops Instructions/Submissions", (".pdf", ".eml", ".txt"),
        "Ops Instructions/Submissions/Processed",
    )]


def test_gather_from_sharepoint_does_not_move_when_mark_processed_is_false(monkeypatch, tmp_path):
    import ops.intake as intake_mod
    monkeypatch.setattr(intake_mod, "OPS_INTAKE_DIR", tmp_path)
    monkeypatch.setattr(intake_mod, "ensure_output_dirs", lambda: None)
    monkeypatch.setattr(sharepoint_client, "is_configured", lambda: True)
    monkeypatch.setattr(settings.sharepoint, "ops_mark_processed", False)

    calls = []

    def fake_fetch_folder(dest_dir, folder_path, extensions=(".pdf",), move_processed_to=None):
        calls.append(move_processed_to)
        return []

    monkeypatch.setattr(sharepoint_client, "fetch_folder", fake_fetch_folder)

    intake_mod.gather_from_sharepoint()

    assert calls == [None]


# --------------------------------------------------- write-back: ops/filer.py

def test_file_document_pushes_to_sharepoint_when_write_back_enabled(monkeypatch, tmp_path):
    import ops.filer as filer_mod
    monkeypatch.setattr(filer_mod, "OPS_FILED_DIR", tmp_path)
    monkeypatch.setattr(filer_mod, "ensure_output_dirs", lambda: None)
    monkeypatch.setattr(settings.sharepoint, "ops_write_back_enabled", True)
    monkeypatch.setattr(settings.sharepoint, "ops_filed_folder", "Ops Filed Documents")

    uploaded = []
    monkeypatch.setattr(sharepoint_client, "upload_file", lambda path, folder: uploaded.append((path, folder)) or True)

    src = tmp_path / "instr.pdf"
    src.write_bytes(b"%PDF-1.4 test")
    doc = _doc("instr.pdf", "WITHDRAWAL_INSTRUCTION", transaction_date="2026-07-10")
    doc.source_path = str(src)
    doc.transaction_key = "TRD123"

    dest = filer_mod.file_document(doc)

    assert len(uploaded) == 1
    uploaded_path, uploaded_folder = uploaded[0]
    assert uploaded_path == dest
    assert uploaded_folder == "Ops Filed Documents/2026/July/10/TRD123"


def test_file_document_skips_sharepoint_when_write_back_disabled(monkeypatch, tmp_path):
    import ops.filer as filer_mod
    monkeypatch.setattr(filer_mod, "OPS_FILED_DIR", tmp_path)
    monkeypatch.setattr(filer_mod, "ensure_output_dirs", lambda: None)
    monkeypatch.setattr(settings.sharepoint, "ops_write_back_enabled", False)

    uploaded = []
    monkeypatch.setattr(sharepoint_client, "upload_file", lambda path, folder: uploaded.append((path, folder)) or True)

    src = tmp_path / "instr.pdf"
    src.write_bytes(b"%PDF-1.4 test")
    doc = _doc("instr.pdf", "WITHDRAWAL_INSTRUCTION", transaction_date="2026-07-10")
    doc.source_path = str(src)
    doc.transaction_key = "TRD123"

    filer_mod.file_document(doc)

    assert uploaded == []


# --------------------------------------------------- write-back: ops/audit_pack.py

def test_compile_pack_pushes_files_to_sharepoint_when_write_back_enabled(monkeypatch, tmp_path):
    import ops.audit_pack as ap
    monkeypatch.setattr(ap, "OPS_AUDIT_DIR", tmp_path)
    monkeypatch.setattr(ap, "ensure_output_dirs", lambda: None)
    monkeypatch.setattr(settings.sharepoint, "ops_write_back_enabled", True)
    monkeypatch.setattr(settings.sharepoint, "ops_audit_folder", "Ops Audit Repository")

    uploaded = []
    monkeypatch.setattr(sharepoint_client, "upload_file", lambda path, folder: uploaded.append((path, folder)) or True)

    src = tmp_path / "instr.pdf"
    src.write_bytes(b"%PDF-1.4 test")
    doc = _doc("instr.pdf", "WITHDRAWAL_INSTRUCTION", trade_id="TRD-77",
               portfolio_code="BPMMF01", transaction_amount="500", transaction_date="2026-07-01")
    doc.source_path = str(src)
    group = correlate([doc])[0]
    pack = evaluate_pack(group)

    folder = compile_pack(pack)

    # One upload per file actually written into the compiled pack folder
    # (the copied document + MANIFEST.txt), all to the same remote folder.
    uploaded_folders = {f for _, f in uploaded}
    uploaded_names = {p.name for p, _ in uploaded}
    assert uploaded_folders == {"Ops Audit Repository/BPMMF01/BPMMF01_TRD-77_2026-07-01"}
    assert uploaded_names == {n.name for n in folder.iterdir() if n.is_file()}


def test_compile_pack_skips_sharepoint_when_write_back_disabled(monkeypatch, tmp_path):
    import ops.audit_pack as ap
    monkeypatch.setattr(ap, "OPS_AUDIT_DIR", tmp_path)
    monkeypatch.setattr(ap, "ensure_output_dirs", lambda: None)
    monkeypatch.setattr(settings.sharepoint, "ops_write_back_enabled", False)

    uploaded = []
    monkeypatch.setattr(sharepoint_client, "upload_file", lambda path, folder: uploaded.append((path, folder)) or True)

    src = tmp_path / "instr.pdf"
    src.write_bytes(b"%PDF-1.4 test")
    doc = _doc("instr.pdf", "WITHDRAWAL_INSTRUCTION", trade_id="TRD-88",
               portfolio_code="BPMMF01", transaction_amount="500", transaction_date="2026-07-01")
    doc.source_path = str(src)
    group = correlate([doc])[0]
    pack = evaluate_pack(group)

    compile_pack(pack)

    assert uploaded == []


if __name__ == "__main__":
    print("Run via pytest - this module uses the monkeypatch/tmp_path fixtures.")
