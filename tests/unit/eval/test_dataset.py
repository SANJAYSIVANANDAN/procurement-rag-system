"""The shipped dataset is what its manifest says, meets the size targets of spec 28.4, and its
labels agree with the documents they describe."""

import re
import shutil
from pathlib import Path

import pytest

from covenantiq.analysis.contract.rules_loader import get_ruleset
from covenantiq.core.enums import ContractType
from eval.dataset import DatasetError, load_dataset, sha256_of

SHIPPED = Path("eval/datasets")


@pytest.fixture(scope="module")
def dataset():  # type: ignore[no-untyped-def]
    return load_dataset()


class TestShape:
    def test_it_meets_the_size_targets(self, dataset) -> None:  # type: ignore[no-untyped-def]
        assert len(dataset.chat) >= 60
        assert sum(1 for e in dataset.chat if not e.answerable) >= 15
        assert len(dataset.findings) >= 12 and len(dataset.vendors) >= 4

    def test_the_mix_covers_follow_ups_and_comparisons(self, dataset) -> None:  # type: ignore[no-untyped-def]
        tags = {t for e in dataset.chat for t in e.tags}
        assert {"follow_up", "multi_doc", "unanswerable"} <= tags
        assert all(
            e.standalone_question and e.conversation_history
            for e in dataset.chat
            if "follow_up" in e.tags
        )

    def test_ids_are_unique(self, dataset) -> None:  # type: ignore[no-untyped-def]
        for rows in (dataset.chat, dataset.findings, dataset.vendors):
            ids = [r.id for r in rows]
            assert len(ids) == len(set(ids))

    def test_the_manifest_records_a_version_sources_and_licences(self, dataset) -> None:  # type: ignore[no-untyped-def]
        manifest = dataset.manifest
        assert manifest.version == "1.0.0" and manifest.sources and manifest.licenses
        assert "documents/acme_saas.txt" in manifest.files and len(manifest.files) == 15


class TestLabelsAgreeWithTheDocuments:
    def text(self, dataset, name: str) -> str:  # type: ignore[no-untyped-def]
        return dataset.document_path(name).read_text(encoding="utf-8")

    def flat(self, text: str) -> str:
        return " ".join(text.split())

    def test_every_relevant_passage_is_in_its_document_word_for_word(self, dataset) -> None:  # type: ignore[no-untyped-def]
        for example in dataset.chat:
            for passage in example.relevant_passages:
                assert self.flat(passage.text) in self.flat(self.text(dataset, passage.document)), (
                    example.id
                )

    def test_every_expected_citation_names_a_document_and_a_clause_that_exist(
        self, dataset
    ) -> None:  # type: ignore[no-untyped-def]
        for example in dataset.chat:
            assert example.document_refs
            for citation in example.expected_citations:
                assert citation.document in example.document_refs
                assert re.search(
                    rf"^{re.escape(citation.clause_ref or '')} ",
                    self.text(dataset, citation.document),
                    re.M,
                ), example.id

    def test_an_unanswerable_question_has_no_passage_and_an_answerable_one_has(
        self, dataset
    ) -> None:  # type: ignore[no-untyped-def]
        for example in dataset.chat:
            assert bool(example.relevant_passages) == example.answerable, example.id

    def test_the_page_a_passage_is_on_is_the_page_in_the_document(self, dataset) -> None:  # type: ignore[no-untyped-def]
        for example in dataset.chat:
            for passage in example.relevant_passages:
                text = self.text(dataset, passage.document)
                before = text[
                    : text.index(passage.text.split(" ")[0] + " " + passage.text.split(" ")[1])
                ]
                pages = re.findall(r"^@page (\d+)$", before, re.M)
                assert int(pages[-1]) == passage.page, example.id

    def test_the_findings_cover_every_rule_that_applies_to_the_contract_type(self, dataset) -> None:  # type: ignore[no-untyped-def]
        ruleset = get_ruleset()
        for example in dataset.findings:
            applicable = {r.id for r in ruleset.applicable(ContractType(example.contract_type))}
            assert {f.rule_id for f in example.expected_findings} == applicable, example.id

    def test_an_absent_rule_means_the_documents_has_no_such_section(self, dataset) -> None:  # type: ignore[no-untyped-def]
        titles = {
            "SLA-001": "Service Levels",
            "AUD-001": "Audit Rights",
            "DP-002": "Breach Notification",
        }
        for example in dataset.findings:
            text = self.text(dataset, example.document)
            for finding in example.expected_findings:
                heading = titles.get(finding.rule_id)
                if heading and finding.status == "absent":
                    assert heading not in text, (example.id, finding.rule_id)
                if finding.status != "absent":
                    assert finding.evidence and self.flat(finding.evidence[0].text) in self.flat(
                        text
                    )

    def test_the_vendor_cases_use_documents_and_signals_that_exist(self, dataset) -> None:  # type: ignore[no-untyped-def]
        from covenantiq.analysis.vendor.config_schema import get_vendor_config

        signals = get_vendor_config().signals.by_id()
        for case in dataset.vendors:
            assert all(dataset.document_path(d).is_file() for d in case.documents)
            assert set(case.expected_signals) <= set(signals)
            assert all(signals[s].from_contract for s in case.expected_signals)

    def test_a_clean_document_and_a_poor_one_differ_in_the_labels(self, dataset) -> None:  # type: ignore[no-untyped-def]
        by_doc = {
            e.document: {f.rule_id: f.status for f in e.expected_findings} for e in dataset.findings
        }
        assert by_doc["acme_saas.txt"]["LIAB-001"] == "acceptable"
        assert by_doc["globex_saas.txt"]["LIAB-001"] == "problematic"
        assert by_doc["globex_saas.txt"]["SLA-001"] == "absent"
        assert by_doc["globex_dpa.txt"]["DP-002"] == "ambiguous"


class TestTheManifestIsEnforced:
    def copy(self, tmp_path: Path) -> Path:
        shutil.copytree(SHIPPED, tmp_path / "datasets")
        return tmp_path / "datasets"

    def test_an_untouched_copy_loads(self, tmp_path: Path) -> None:
        assert load_dataset(root=self.copy(tmp_path)).manifest.version == "1.0.0"

    def test_an_edited_file_is_refused(self, tmp_path: Path) -> None:
        root = self.copy(tmp_path)
        target = root / "v1.0.0" / "documents" / "acme_saas.txt"
        target.write_text(
            target.read_text(encoding="utf-8") + "\nA new clause.\n", encoding="utf-8"
        )
        with pytest.raises(DatasetError, match=r"changed: documents/acme_saas\.txt"):
            load_dataset(root=root)

    def test_a_deleted_file_is_refused(self, tmp_path: Path) -> None:
        root = self.copy(tmp_path)
        (root / "v1.0.0" / "vendor_cases.jsonl").unlink()
        with pytest.raises(DatasetError, match=r"missing: vendor_cases\.jsonl"):
            load_dataset(root=root)

    def test_a_file_the_manifest_does_not_list_is_refused(self, tmp_path: Path) -> None:
        root = self.copy(tmp_path)
        (root / "v1.0.0" / "documents" / "sneaky.txt").write_text("x", encoding="utf-8")
        with pytest.raises(DatasetError, match=r"not in the manifest: documents/sneaky\.txt"):
            load_dataset(root=root)

    def test_a_missing_manifest_is_an_error(self, tmp_path: Path) -> None:
        root = self.copy(tmp_path)
        (root / "v1.0.0" / "manifest.yaml").unlink()
        with pytest.raises(DatasetError, match="cannot read the manifest"):
            load_dataset(root=root)

    def test_the_error_says_how_to_put_it_right(self, tmp_path: Path) -> None:
        root = self.copy(tmp_path)
        (root / "v1.0.0" / "chat_qa.jsonl").write_text("", encoding="utf-8")
        with pytest.raises(DatasetError, match="generate_v1"):
            load_dataset(root=root)

    def test_the_recorded_hashes_are_real_sha256_of_the_files(self) -> None:
        manifest = load_dataset().manifest
        for relative, digest in manifest.files.items():
            assert re.fullmatch(r"[0-9a-f]{64}", digest)
            assert sha256_of(SHIPPED / "v1.0.0" / relative) == digest


class TestTheGeneratorIsDeterministic:
    def test_running_it_again_changes_nothing(self, tmp_path: Path) -> None:
        import hashlib

        from eval.datasets import generate_v1

        before = {
            p.relative_to(generate_v1.ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in generate_v1.ROOT.rglob("*")
            if p.is_file()
        }
        generate_v1.main()
        after = {
            p.relative_to(generate_v1.ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in generate_v1.ROOT.rglob("*")
            if p.is_file()
        }
        assert before == after
