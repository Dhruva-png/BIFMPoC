"""
Tests for the Use Case 2 (BIFM Ops) application - every deterministic
component: portfolio mapping, correlation, audit-pack evaluation and
compilation, Year/Month/Date/Transaction filing, and the metadata
repository's search. analyze_item's own deterministic post-processing
(portfolio fallback, batch salesperson) is covered with ask_text mocked
to a canned response; live extraction accuracy against a real document
still needs a real run.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ops.analyzer as analyzer_mod
from ops.analyzer import _is_weak, _normalize_amount, _normalize_date, analyze_item
from ops.audit_pack import compile_pack, evaluate_pack, zip_audit_folders, zip_packs_for_salesperson
from ops.correlator import correlate
from ops.intake import IntakeItem
from ops.models import OpsDocument
from ops.portfolio import resolve_portfolio
from ops.repository import (
    _init,
    documents_for_transaction,
    list_salespeople,
    list_transactions,
    save_batch,
    search_documents,
)
import ops.salesperson as salesperson_mod
from ops.salesperson import SOURCE_ASSIGNED, SOURCE_EXTRACTED, SOURCE_NONE, resolve_salesperson


def _doc(name, doc_type="TRADE_ORDER", **meta) -> OpsDocument:
    d = OpsDocument(source_file=name, source_path=f"/tmp/{name}", doc_type_code=doc_type,
                    doc_type_name=doc_type.replace("_", " ").title())
    d.metadata.update({k: str(v) for k, v in meta.items()})
    return d


def _shared_doc(name, doc_type, line_items) -> OpsDocument:
    """A shared-ledger document (BIFM's real daily Cash/Trade Order batch
    reports) - line_items is a list of row dicts, each already resolved
    the way ops.analyzer.analyze_item would leave them."""
    d = OpsDocument(source_file=name, source_path=f"/tmp/{name}", doc_type_code=doc_type,
                    doc_type_name=doc_type.replace("_", " ").title())
    d.line_items = [{k: str(v) for k, v in item.items()} for item in line_items]
    return d


# ------------------------------------------------------------- portfolio

def test_portfolio_resolves_exact_code_name_and_alias():
    assert resolve_portfolio("BPMMF01") == ("BPMMF01", "BIFM Pula Money Market Fund")
    assert resolve_portfolio("BIFM Pula Money Market Fund")[0] == "BPMMF01"
    assert resolve_portfolio("GSGF")[0] == "BGSGF06"


def test_portfolio_resolves_loose_client_phrasing():
    # Clients write names loosely in emails - containment must still resolve.
    assert resolve_portfolio("the Pula Money Market fund please")[0] == "BPMMF01"


def test_portfolio_fuzzy_typo_resolves_unambiguously():
    assert resolve_portfolio("Pula Money Marketc Fund")[0] == "BPMMF01"


def test_unknown_portfolio_is_not_guessed():
    assert resolve_portfolio("Orange Bicycle Fund") == ("", "")
    assert resolve_portfolio("") == ("", "")


# ------------------------------------------------------------- correlator

def test_documents_sharing_a_trade_id_form_one_transaction():
    docs = [
        _doc("instr.pdf", "WITHDRAWAL_INSTRUCTION", trade_id="TRD-2026-001",
             portfolio_code="BPMMF01", transaction_amount="25000"),
        _doc("order.pdf", "TRADE_ORDER", trade_id="trd 2026 001",  # loose formatting
             portfolio_code="BPMMF01", transaction_amount="25000"),
    ]
    groups = correlate(docs)
    assert len(groups) == 1
    assert groups[0].transaction_key == "TRD2026001"
    assert groups[0].transaction_type == "Withdrawal"
    assert docs[0].transaction_key == docs[1].transaction_key


def test_bank_statement_without_trade_id_joins_by_portfolio_and_amount():
    # The proposal's correlation identifiers: a bank statement rarely quotes
    # the trade id but shows the same amount on the same portfolio.
    docs = [
        _doc("instr.pdf", "WITHDRAWAL_INSTRUCTION", trade_id="TRD-9",
             portfolio_code="BPMMF01", transaction_amount="1200",
             transaction_date="2026-07-10"),
        _doc("bank.pdf", "BANK_STATEMENT",
             portfolio_code="BPMMF01", transaction_amount="1200",
             transaction_date="2026-07-12"),
    ]
    groups = correlate(docs)
    assert len(groups) == 1
    assert {d.source_file for d in groups[0].documents} == {"instr.pdf", "bank.pdf"}


def test_far_apart_dates_do_not_correlate_on_composite_key():
    docs = [
        _doc("a.pdf", "PROOF_OF_PAYMENT", portfolio_code="BBPF02",
             transaction_amount="5000", transaction_date="2026-01-05"),
        _doc("b.pdf", "PROOF_OF_PAYMENT", portfolio_code="BBPF02",
             transaction_amount="5000", transaction_date="2026-06-20"),
    ]
    groups = correlate(docs)
    assert len(groups) == 2  # same portfolio+amount, months apart -> different transactions


def test_uncorrelatable_document_is_flagged_not_forced():
    docs = [_doc("mystery.pdf", "BANK_STATEMENT")]  # no trade id, portfolio, or amount
    groups = correlate(docs)
    assert len(groups) == 1
    assert groups[0].transaction_key.startswith("UNMATCHED")
    assert any("correlated" in f for f in docs[0].review_flags)


# ---------------------------------------------- shared-ledger multi-row correlation
# BIFM's real daily Cash and Trade Order batch reports cover several
# portfolios/transactions in one document (confirmed against BIFM's
# actual sample documents), unlike the usual one-document-one-transaction
# case these tests otherwise exercise.

def test_shared_ledger_row_attaches_to_matching_single_row_transaction():
    instruction = _doc("instr.pdf", "WITHDRAWAL_INSTRUCTION",
                        portfolio_code="LIBGLO", transaction_amount="13392853.26",
                        transaction_date="2026-07-02")
    cash = _shared_doc("cash.pdf", "CASH_FLOW_STATEMENT", line_items=[
        {"portfolio_code": "LIBGLO", "portfolio_name": "Liberty Global Balanced",
         "transaction_amount": "13392853.26", "transaction_date": "2026-07-06", "trade_id": ""},
        {"portfolio_code": "SOMEOTHER", "portfolio_name": "Some Other Fund",
         "transaction_amount": "999", "transaction_date": "2026-07-06", "trade_id": ""},
    ])
    groups = correlate([instruction, cash])
    # LIBGLO's row joins the matching instruction; SOMEOTHER's row matches
    # nothing else in this batch, so - like any lone document - it forms
    # its own separate transaction group rather than being dropped.
    assert len(groups) == 2
    libglo_group = next(g for g in groups if g.portfolio_code == "LIBGLO")
    assert {d.source_file for d in libglo_group.documents} == {"instr.pdf", "cash.pdf"}
    # The attached copy is a CLONE carrying just the LIBGLO row's values,
    # not the shared document's own (blank) top-level metadata.
    cash_clone = next(d for d in libglo_group.documents if d.source_file == "cash.pdf")
    assert cash_clone.meta("portfolio_code") == "LIBGLO"
    assert cash_clone.meta("transaction_amount") == "13392853.26"
    someother_group = next(g for g in groups if g.portfolio_code == "SOMEOTHER")
    assert [d.source_file for d in someother_group.documents] == ["cash.pdf"]


def test_shared_ledger_document_attaches_to_multiple_transactions():
    # This is the exact real-world case this feature exists for: one
    # physical Cash_070726.pdf file is evidence for BOTH LIBGLO's and
    # LIBLMM's separate withdrawals.
    instr_a = _doc("a.pdf", "WITHDRAWAL_INSTRUCTION", portfolio_code="LIBGLO",
                    transaction_amount="13392853.26", transaction_date="2026-07-02")
    instr_b = _doc("b.pdf", "WITHDRAWAL_INSTRUCTION", portfolio_code="LIBLMM",
                    transaction_amount="314621.82", transaction_date="2026-07-02")
    cash = _shared_doc("cash.pdf", "CASH_FLOW_STATEMENT", line_items=[
        {"portfolio_code": "LIBGLO", "portfolio_name": "", "transaction_amount": "13392853.26",
         "transaction_date": "2026-07-07", "trade_id": ""},
        {"portfolio_code": "LIBLMM", "portfolio_name": "", "transaction_amount": "314621.82",
         "transaction_date": "2026-07-07", "trade_id": ""},
    ])
    groups = correlate([instr_a, instr_b, cash])
    assert len(groups) == 2
    cash_clones = [d for g in groups for d in g.documents if d.source_file == "cash.pdf"]
    assert len(cash_clones) == 2
    assert {c.meta("portfolio_code") for c in cash_clones} == {"LIBGLO", "LIBLMM"}
    # Same physical file referenced from both transactions...
    assert all(c.source_path == cash.source_path for c in cash_clones)
    # ...but independent clones, each keyed to its own transaction.
    assert cash_clones[0].transaction_key != cash_clones[1].transaction_key


def test_shared_ledger_row_with_no_match_still_forms_its_own_transaction():
    # A row that resolves to a real portfolio but matches nothing else in
    # the batch is real (if incomplete) audit evidence, not noise - it
    # gets its own transaction group exactly like a lone document would,
    # rather than being silently dropped.
    cash = _shared_doc("cash.pdf", "CASH_FLOW_STATEMENT", line_items=[
        {"portfolio_code": "BTCLPF", "portfolio_name": "", "transaction_amount": "284448.98",
         "transaction_date": "2026-07-06", "trade_id": ""},
    ])
    groups = correlate([cash])
    assert len(groups) == 1
    assert groups[0].portfolio_code == "BTCLPF"
    assert not groups[0].transaction_key.startswith("UNMATCHED")
    clone = groups[0].documents[0]
    assert clone is not cash  # a materialized clone, not the shared original
    assert clone.source_path == cash.source_path  # same physical file


def test_shared_ledger_with_zero_resolvable_rows_degrades_to_whole_document():
    # ops.analyzer never populates line_items for a row it couldn't
    # resolve a portfolio for, so a shared document where nothing
    # resolved looks just like line_items=[] here - it must degrade to
    # ordinary whole-document handling (one flagged unmatched group for
    # itself), not vanish or error.
    cash = _shared_doc("cash.pdf", "CASH_FLOW_STATEMENT", line_items=[])
    groups = correlate([cash])
    assert len(groups) == 1
    assert groups[0].transaction_key.startswith("UNMATCHED")
    assert groups[0].documents == [cash]  # the original object, not a clone
    assert any("correlated" in f for f in cash.review_flags)


def test_shared_ledger_row_can_seed_its_own_transaction():
    # The shared ledger is processed in the same batch as its matching
    # letter, but nothing guarantees which is analysed "first" - a row
    # unit must be able to seed a brand new composite group exactly like
    # a whole document can, not only ever absorb into a pre-existing one.
    cash = _shared_doc("cash.pdf", "CASH_FLOW_STATEMENT", line_items=[
        {"portfolio_code": "LIBGLO", "portfolio_name": "", "transaction_amount": "13392853.26",
         "transaction_date": "2026-07-07", "trade_id": ""},
    ])
    letter = _doc("letter.pdf", "WITHDRAWAL_INSTRUCTION", portfolio_code="LIBGLO",
                   transaction_amount="13392853.26", transaction_date="2026-07-02")
    groups = correlate([cash, letter])
    assert len(groups) == 1
    assert {d.source_file for d in groups[0].documents} == {"cash.pdf", "letter.pdf"}


def test_unknown_transaction_type_pack_is_not_reported_complete():
    # A transaction of unknown type has no pack definition to satisfy -
    # its status must say that, not claim "Complete".
    pack = evaluate_pack(correlate([_doc("mystery.pdf", "BANK_STATEMENT")])[0])
    assert not pack.is_complete
    assert "No pack defined" in pack.status


def test_contribution_type_inferred_from_document_mix():
    docs = [
        _doc("pop.pdf", "PROOF_OF_PAYMENT", portfolio_code="BBPF02", transaction_amount="800"),
        _doc("transfer.pdf", "PROOF_OF_TRANSFER", portfolio_code="BBPF02", transaction_amount="800"),
    ]
    groups = correlate(docs)
    assert groups[0].transaction_type == "Contribution"


# ------------------------------------------------------------- audit packs

def _withdrawal_group(codes):
    docs = [_doc(f"{c.lower()}.pdf", c, trade_id="T1", portfolio_code="BPMMF01",
                 transaction_amount="100") for c in codes]
    return correlate(docs)[0]


def test_complete_withdrawal_pack():
    group = _withdrawal_group(
        ["WITHDRAWAL_INSTRUCTION", "APPROVAL_EMAIL", "TRADE_ORDER", "CASH_FLOW_STATEMENT", "BANK_STATEMENT"])
    pack = evaluate_pack(group)
    assert pack.is_complete
    assert pack.status == "Complete"


def test_missing_required_item_marks_pack_incomplete():
    # TRADE_ORDER is 'where applicable' (absent for segregated-client
    # withdrawals - no fund trade to record), so a genuinely incomplete
    # pack needs to be missing something that's actually always required.
    group = _withdrawal_group(["WITHDRAWAL_INSTRUCTION", "APPROVAL_EMAIL", "TRADE_ORDER"])
    pack = evaluate_pack(group)
    assert not pack.is_complete
    missing = {i.code for i in pack.missing_required}
    assert "CASH_FLOW_STATEMENT" in missing


def test_where_applicable_item_missing_does_not_fail_the_pack():
    # Bank Statement / Proof of Payment on withdrawals is 'where applicable'.
    group = _withdrawal_group(
        ["WITHDRAWAL_INSTRUCTION", "APPROVAL_EMAIL", "TRADE_ORDER", "CASH_FLOW_STATEMENT"])
    pack = evaluate_pack(group)
    assert pack.is_complete


def test_satisfied_by_alternates_deposit_confirmation_counts_as_proof_of_payment():
    docs = [
        _doc("dep.pdf", "DEPOSIT_CONFIRMATION", trade_id="C1", portfolio_code="BBPF02", transaction_amount="900"),
        _doc("bank.pdf", "BANK_STATEMENT", trade_id="C1", portfolio_code="BBPF02", transaction_amount="900"),
        _doc("tr.pdf", "PROOF_OF_TRANSFER", trade_id="C1", portfolio_code="BBPF02", transaction_amount="900"),
        _doc("cash.pdf", "CASH_TEMPLATE", trade_id="C1", portfolio_code="BBPF02", transaction_amount="900"),
    ]
    pack = evaluate_pack(correlate(docs)[0])
    pop_item = next(i for i in pack.items if i.code == "PROOF_OF_PAYMENT")
    assert pop_item.present and pop_item.satisfied_by_code == "DEPOSIT_CONFIRMATION"
    assert pack.is_complete


def test_compile_pack_builds_folder_by_portfolio_and_naming_convention(tmp_path, monkeypatch):
    import ops.audit_pack as ap
    monkeypatch.setattr(ap, "OPS_AUDIT_DIR", tmp_path)
    monkeypatch.setattr(ap, "ensure_output_dirs", lambda: None)

    src = tmp_path / "instr.pdf"
    src.write_bytes(b"%PDF-1.4 test")
    doc = _doc("instr.pdf", "WITHDRAWAL_INSTRUCTION", trade_id="TRD-77",
               portfolio_code="BPMMF01", transaction_amount="500", transaction_date="2026-07-01")
    doc.source_path = str(src)
    group = correlate([doc])[0]
    pack = evaluate_pack(group)
    folder = compile_pack(pack)

    # Organised by Portfolio Code, named {portfolio_code}_{transaction_id}_{transaction_date}.
    assert folder.parent.name == "BPMMF01"
    assert folder.name == "BPMMF01_TRD-77_2026-07-01"
    assert (folder / "MANIFEST.txt").exists()
    manifest = (folder / "MANIFEST.txt").read_text(encoding="utf-8")
    assert "MISSING" in manifest  # incomplete pack says so right in the folder
    assert (folder / "WITHDRAWAL_INSTRUCTION_instr.pdf").exists()


# ------------------------------------------------------------- filing

def test_filing_follows_year_month_date_transaction_structure(tmp_path, monkeypatch):
    import ops.filer as filer_mod
    monkeypatch.setattr(filer_mod, "OPS_FILED_DIR", tmp_path)
    monkeypatch.setattr(filer_mod, "ensure_output_dirs", lambda: None)

    src = tmp_path / "instr.pdf"
    src.write_bytes(b"%PDF-1.4 test")
    doc = _doc("instr.pdf", "WITHDRAWAL_INSTRUCTION", transaction_date="2026-07-10")
    doc.source_path = str(src)
    doc.transaction_key = "TRD123"

    dest = filer_mod.file_document(doc)
    assert dest.exists()
    # Year -> Month -> Date -> Transaction, per BIFM's business rules.
    assert dest.parent.name == "TRD123"
    assert dest.parent.parent.name == "10"
    assert dest.parent.parent.parent.name == "July"
    assert dest.parent.parent.parent.parent.name == "2026"
    assert doc.filed_path == str(dest)


# ------------------------------------------------------------- repository

def test_repository_search_by_any_metadata_and_by_field(tmp_path):
    db = tmp_path / "ops.db"
    docs = [
        _doc("instr.pdf", "WITHDRAWAL_INSTRUCTION", trade_id="TRD-55",
             portfolio_code="BPMMF01", client_name="Theo Mabe", transaction_amount="25000"),
        _doc("bank.pdf", "BANK_STATEMENT", trade_id="TRD-55",
             portfolio_code="BPMMF01", client_name="Theo Mabe", transaction_amount="25000"),
    ]
    for i, d in enumerate(docs):
        d.filed_path = f"/tmp/filed_{i}.pdf"
    groups = correlate(docs)
    packs = [evaluate_pack(g) for g in groups]
    save_batch(docs, packs, db_path=db)

    # Free-text search hits any metadata column.
    assert len(search_documents("Theo", db_path=db)) == 2
    assert len(search_documents("TRD-55", db_path=db)) == 2
    # Field-scoped search.
    assert len(search_documents("BPMMF01", field="portfolio_code", db_path=db)) == 2
    assert search_documents("BPMMF01", field="client_name", db_path=db) == []
    # All documents for a transaction.
    key = docs[0].transaction_key
    assert len(documents_for_transaction(key, db_path=db)) == 2
    # Transactions listing carries the pack status.
    tx = list_transactions(db_path=db)
    assert len(tx) == 1 and tx[0]["transaction_key"] == key


def test_repository_upsert_does_not_duplicate_on_rerun(tmp_path):
    db = tmp_path / "ops.db"
    doc = _doc("instr.pdf", "WITHDRAWAL_INSTRUCTION", trade_id="T9",
               portfolio_code="BBPF02", transaction_amount="10")
    doc.filed_path = "/tmp/filed_x.pdf"
    groups = correlate([doc])
    packs = [evaluate_pack(g) for g in groups]
    save_batch([doc], packs, db_path=db)
    save_batch([doc], packs, db_path=db)  # same batch again
    assert len(search_documents("T9", db_path=db)) == 1


# ------------------------------------------------------------- analyzer normalisers

def test_is_weak_flags_unrecognized_and_empty_reads():
    # UNRECOGNIZED is always weak, whatever metadata came with it.
    assert _is_weak({"doc_type": "UNRECOGNIZED", "metadata": {"client_name": {"value": "Theo Mabe"}}})
    # Classified, but every core field null - a page-1-only read of a form
    # whose cover page (page 1) carries no investor/fund/amount data at all.
    assert _is_weak({"doc_type": "TRADE_ORDER", "metadata": {"security_name": {"value": "N/A"}}})
    assert _is_weak({"doc_type": "TRADE_ORDER", "metadata": {}})


def test_is_weak_false_once_a_core_field_resolved():
    assert not _is_weak({
        "doc_type": "TRADE_ORDER",
        "metadata": {"portfolio": {"value": "BIFM Pula Money Market Fund"}},
    })
    assert not _is_weak({
        "doc_type": "TRADE_ORDER",
        "metadata": {"trade_id": "T-123"},  # bare value, not a {value, confidence} payload
    })


def test_date_and_amount_normalisation():
    assert _normalize_date("10/07/2026") == "2026-07-10"
    assert _normalize_date("10 July 2026") == "2026-07-10"
    assert _normalize_date("") == ""
    assert _normalize_amount("P25,000.00") == "25000"
    assert _normalize_amount("1 200") == "1200"
    assert _normalize_amount("") == ""


class _FakeLLMResponse:
    def __init__(self, text: str):
        self.text = text


def test_portfolio_falls_back_to_security_name_when_raw_field_is_a_form_reference_number(monkeypatch):
    # Real bug, seen against a real BIFM UT Disinvestment Form: the model
    # read the printed "Fund Number" column (a client form's own internal
    # reference, e.g. "178231") as "portfolio" instead of the fund name in
    # the very next column - that number will never be in the Portfolio
    # Name-to-Code mapping. security_name is read from the same table and
    # correctly names the fund - analyze_item should retry resolution
    # against that before giving up.
    canned = {
        "doc_type": "WITHDRAWAL_INSTRUCTION", "confidence": 98,
        "metadata": {
            "portfolio": {"value": "178231", "confidence": 90},
            "client_name": {"value": "Onalenna Bogosing", "confidence": 95},
            "transaction_type": {"value": "Withdrawal", "confidence": 90},
            "transaction_amount": {"value": "40000", "confidence": 95},
            "security_name": {"value": "BIFM Global Sustainable Growth Fund", "confidence": 90},
        },
        "line_items": [],
    }
    monkeypatch.setattr(analyzer_mod, "ask_text", lambda *a, **kw: _FakeLLMResponse(json.dumps(canned)))

    item = IntakeItem(path=Path("GSGF - ONALENNA.txt"), kind="text", source_label="Folder drop",
                       body_text="disinvestment instruction body")
    doc = analyze_item(item)

    assert doc.metadata["portfolio_code"] == "BGSGF06"
    assert doc.metadata["portfolio_name"] == "BIFM Global Sustainable Growth Fund"
    assert not any("not found in the Portfolio Name-to-Code mapping" in f for f in doc.review_flags)


def test_batch_fallback_salesperson_used_when_nothing_else_resolves(monkeypatch):
    canned = {
        "doc_type": "WITHDRAWAL_INSTRUCTION", "confidence": 98,
        "metadata": {"client_name": {"value": "Someone", "confidence": 90}},
        "line_items": [],
    }
    monkeypatch.setattr(analyzer_mod, "ask_text", lambda *a, **kw: _FakeLLMResponse(json.dumps(canned)))
    item = IntakeItem(path=Path("x.txt"), kind="text", source_label="Folder drop", body_text="body")

    doc = analyze_item(item, batch_fallback_salesperson="Naledi Phiri")
    assert doc.metadata["salesperson"] == "Naledi Phiri"


def test_batch_fallback_salesperson_loses_to_a_real_portfolio_match(monkeypatch):
    # The portfolio-roster mapping is a stronger signal than the batch's
    # arbitrary random pick - it should still win when both are available.
    canned = {
        "doc_type": "WITHDRAWAL_INSTRUCTION", "confidence": 98,
        "metadata": {
            "portfolio": {"value": "BIFM Pula Money Market Fund", "confidence": 95},
            "transaction_amount": {"value": "1000", "confidence": 90},
        },
        "line_items": [],
    }
    monkeypatch.setattr(analyzer_mod, "ask_text", lambda *a, **kw: _FakeLLMResponse(json.dumps(canned)))
    item = IntakeItem(path=Path("y.txt"), kind="text", source_label="Folder drop", body_text="body")

    # BPMMF01 maps to Thabo Kgosi in the demo roster - batch_fallback is a
    # different name and must not override that real match.
    doc = analyze_item(item, batch_fallback_salesperson="Kagiso Mmusi")
    assert doc.metadata["salesperson"] == "Thabo Kgosi"


def test_page_1_weak_read_on_a_genuine_one_page_pdf_does_not_crash(monkeypatch):
    # Regression: a bare one-page scan (e.g. a KYC ID document with no
    # matching UC2 doc type) legitimately reads as UNRECOGNIZED from page
    # 1 alone, which is "weak" and triggers the page-2 retry - but this
    # PDF genuinely has no page 2. render_pdf_to_images raises in that
    # case (a requested page that doesn't exist), which must not blow up
    # the whole document's analysis over an optional secondary attempt.
    weak = {"doc_type": "UNRECOGNIZED", "confidence": 98, "metadata": {}, "line_items": []}
    monkeypatch.setattr(analyzer_mod, "ask_vision", lambda *a, **kw: _FakeLLMResponse(json.dumps(weak)))

    def fake_render(path, output_subdir=None, page_numbers=None):
        if page_numbers == [1]:
            return {1: Path("fake_page_1.png")}
        raise ValueError(f"No pages rendered from {path.name}")  # no page 2 exists

    monkeypatch.setattr(analyzer_mod, "render_pdf_to_images", fake_render)
    item = IntakeItem(path=Path("KYC - ONALENNA.pdf"), kind="pdf", source_label="Folder drop")

    doc = analyze_item(item)
    assert not doc.error
    assert doc.doc_type_code == "UNRECOGNIZED"


# ------------------------------------------------------------- salesperson (demo capability)
# The demo roster (ops/config/salesperson_roster.json) maps:
#   BPMMF01, BLEF03  -> Thabo Kgosi
#   BBPF02, BYMJF04  -> Naledi Phiri
#   BLEQ05, BGSGF06  -> Kagiso Mmusi

def test_stated_salesperson_always_wins_over_the_roster():
    # A document that actually names its advisor is real data - the demo
    # roster must never override it, even though BPMMF01 maps to someone else.
    name, source = resolve_salesperson("Naledi Phiri", "BPMMF01")
    assert (name, source) == ("Naledi Phiri", SOURCE_EXTRACTED)


def test_roster_fallback_when_nothing_stated():
    name, source = resolve_salesperson("", "BPMMF01")
    assert (name, source) == ("Thabo Kgosi", SOURCE_ASSIGNED)
    name2, source2 = resolve_salesperson(None, "BBPF02")
    assert (name2, source2) == ("Naledi Phiri", SOURCE_ASSIGNED)


def test_no_portfolio_still_gets_a_random_roster_assignment():
    # Unlike ops.portfolio's stricter rule, salesperson is an explicit demo
    # capability (see ops/salesperson.py's docstring) - a portfolio-less
    # document still gets someone from the roster, at random, so every pack
    # built in the demo has a name to show rather than a blank field.
    roster_names = {"Thabo Kgosi", "Naledi Phiri", "Kagiso Mmusi"}
    name, source = resolve_salesperson("", "")
    assert source == SOURCE_ASSIGNED and name in roster_names
    name2, source2 = resolve_salesperson(None, None)
    assert source2 == SOURCE_ASSIGNED and name2 in roster_names


def test_portfolio_not_in_roster_falls_back_to_random_assignment():
    roster_names = {"Thabo Kgosi", "Naledi Phiri", "Kagiso Mmusi"}
    name, source = resolve_salesperson("", "UNKNOWN_CODE")
    assert source == SOURCE_ASSIGNED and name in roster_names


def test_empty_roster_still_means_no_assignment(monkeypatch):
    monkeypatch.setattr(salesperson_mod, "load_salesperson_roster", lambda: [])
    assert resolve_salesperson("", "BPMMF01") == ("", SOURCE_NONE)


def test_correlator_propagates_salesperson_onto_the_transaction():
    docs = [
        _doc("a.pdf", "WITHDRAWAL_INSTRUCTION", trade_id="T1", portfolio_code="BPMMF01",
             transaction_amount="100", salesperson="Thabo Kgosi"),
        _doc("b.pdf", "TRADE_ORDER", trade_id="T1", portfolio_code="BPMMF01",
             transaction_amount="100"),  # doesn't state one - group still resolves
    ]
    group = correlate(docs)[0]
    assert group.salesperson == "Thabo Kgosi"


# ------------------------------------------------------------- repository: salesperson filter

def test_list_transactions_and_list_salespeople_filter_correctly(tmp_path):
    db = tmp_path / "ops.db"
    doc_a = _doc("a.pdf", "WITHDRAWAL_INSTRUCTION", trade_id="T-A", portfolio_code="BPMMF01",
                 transaction_amount="100", salesperson="Thabo Kgosi")
    doc_b = _doc("b.pdf", "PROOF_OF_PAYMENT", trade_id="T-B", portfolio_code="BBPF02",
                 transaction_amount="200", salesperson="Naledi Phiri")
    groups = correlate([doc_a]) + correlate([doc_b])
    packs = [evaluate_pack(g) for g in groups]
    save_batch([doc_a, doc_b], packs, db_path=db)

    assert sorted(list_salespeople(db_path=db)) == ["Naledi Phiri", "Thabo Kgosi"]
    thabo_only = list_transactions(db_path=db, salesperson="Thabo Kgosi")
    assert len(thabo_only) == 1 and thabo_only[0]["client_name"] == doc_a.meta("client_name")
    assert list_transactions(db_path=db, salesperson="Nobody Here") == []
    assert len(list_transactions(db_path=db)) == 2  # unfiltered still returns everything


def test_repository_migrates_a_database_created_before_salesperson_existed(tmp_path):
    # Regression guard for the exact bug this feature's own rollout hit:
    # CREATE TABLE IF NOT EXISTS is a no-op against a database that already
    # exists, so a repository.db from before "salesperson" was added would
    # otherwise break every query referencing that column. Build a
    # deliberately old-schema DB (no salesperson column anywhere) and
    # confirm _init() adds it in place without losing existing rows.
    import sqlite3
    db = tmp_path / "old_schema.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT, filed_path TEXT, portfolio_code TEXT,
            UNIQUE(filed_path)
        )""")
    conn.execute("""
        CREATE TABLE transactions (
            transaction_key TEXT PRIMARY KEY, portfolio_code TEXT, client_name TEXT
        )""")
    conn.execute("INSERT INTO documents (source_file, filed_path, portfolio_code) VALUES (?,?,?)",
                 ("old.pdf", "/filed/old.pdf", "BPMMF01"))
    conn.execute("INSERT INTO transactions (transaction_key, portfolio_code, client_name) VALUES (?,?,?)",
                 ("OLDKEY", "BPMMF01", "Old Client"))
    conn.commit()
    conn.close()

    conn2 = sqlite3.connect(db)
    _init(conn2)  # the migration under test

    doc_cols = {r[1] for r in conn2.execute("PRAGMA table_info(documents)")}
    tx_cols = {r[1] for r in conn2.execute("PRAGMA table_info(transactions)")}
    assert "salesperson" in doc_cols
    assert "salesperson" in tx_cols

    # Original rows survive, untouched, with salesperson simply NULL/blank -
    # never fabricated for data that predates the field.
    row = conn2.execute("SELECT source_file, portfolio_code, salesperson FROM documents").fetchone()
    assert row[0] == "old.pdf" and row[1] == "BPMMF01" and not row[2]
    tx_row = conn2.execute("SELECT client_name, salesperson FROM transactions").fetchone()
    assert tx_row[0] == "Old Client" and not tx_row[1]
    conn2.close()


# ------------------------------------------------------------- audit-pack zip export

def test_zip_audit_folders_includes_files_and_skips_missing(tmp_path):
    folder_a = tmp_path / "PACK_A"
    folder_a.mkdir()
    (folder_a / "MANIFEST.txt").write_text("manifest a", encoding="utf-8")
    (folder_a / "doc.txt").write_text("doc a", encoding="utf-8")

    missing_folder = str(tmp_path / "DOES_NOT_EXIST")

    zip_bytes = zip_audit_folders([str(folder_a), missing_folder, ""])

    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
    assert names == {"PACK_A/MANIFEST.txt", "PACK_A/doc.txt"}


def test_zip_packs_for_salesperson_only_includes_their_transactions(tmp_path):
    folder_thabo = tmp_path / "THABO_PACK"
    folder_thabo.mkdir()
    (folder_thabo / "MANIFEST.txt").write_text("m", encoding="utf-8")
    folder_naledi = tmp_path / "NALEDI_PACK"
    folder_naledi.mkdir()
    (folder_naledi / "MANIFEST.txt").write_text("m", encoding="utf-8")

    doc_a = _doc("a.pdf", "WITHDRAWAL_INSTRUCTION", trade_id="T-A", portfolio_code="BPMMF01",
                 transaction_amount="100", salesperson="Thabo Kgosi")
    doc_b = _doc("b.pdf", "PROOF_OF_PAYMENT", trade_id="T-B", portfolio_code="BBPF02",
                 transaction_amount="200", salesperson="Naledi Phiri")
    pack_thabo = evaluate_pack(correlate([doc_a])[0])
    pack_thabo.audit_folder = str(folder_thabo)
    pack_naledi = evaluate_pack(correlate([doc_b])[0])
    pack_naledi.audit_folder = str(folder_naledi)

    zip_bytes = zip_packs_for_salesperson([pack_thabo, pack_naledi], "Thabo Kgosi")

    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
    assert names == ["THABO_PACK/MANIFEST.txt"]  # only Thabo's pack, not Naledi's
