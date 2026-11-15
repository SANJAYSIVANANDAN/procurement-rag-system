"""Loading an evaluation dataset, and refusing one whose files do not match its manifest.

The manifest records a SHA-256 for every file. A run that used a silently edited dataset would
compare itself to a baseline made on another one, so a mismatch stops the run (spec 28.6).
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

DATASETS_ROOT = Path(__file__).parent / "datasets"
DEFAULT_VERSION = "v1.0.0"


class DatasetError(RuntimeError):
    pass


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Passage(_Row):
    document: str
    page: int | None = None
    clause_ref: str | None = None
    text: str = ""


class Turn(_Row):
    role: Literal["user", "assistant"]
    content: str


class ChatExample(_Row):
    id: str
    document_refs: list[str]
    question: str
    standalone_question: str | None = None  # what a model rewrites a follow-up into
    conversation_history: list[Turn] = Field(default_factory=list)
    answerable: bool
    ground_truth_answer: str
    relevant_passages: list[Passage]
    expected_citations: list[Passage]
    tags: list[str] = Field(default_factory=list)


class ExpectedFinding(_Row):
    rule_id: str
    status: Literal["absent", "problematic", "ambiguous", "insufficient_evidence", "acceptable"]
    evidence: list[Passage] = Field(default_factory=list)


class FindingsExample(_Row):
    id: str
    document: str
    contract_type: str
    vendor: str
    expected_findings: list[ExpectedFinding]
    not_expected: list[str] = Field(default_factory=list)  # "RULE:status" that must not appear


class VendorCase(_Row):
    id: str
    vendor: str
    contract_type_note: str = ""
    documents: list[str]
    expected_signals: dict[str, str]
    expected_dimension_order: dict[str, str] = Field(default_factory=dict)


class Manifest(_Row):
    version: str
    schema_version: int
    created: str
    description: str
    sources: list[str]
    licenses: list[str]
    files: dict[str, str]


class Dataset(_Row):
    root: Path
    manifest: Manifest
    chat: list[ChatExample]
    findings: list[FindingsExample]
    vendors: list[VendorCase]

    def document_path(self, name: str) -> Path:
        return self.root / "documents" / name

    def documents_used(self) -> set[str]:
        """The documents that some example in this dataset refers to."""
        names = {d for e in self.chat for d in e.document_refs}
        names |= {f.document for f in self.findings}
        names |= {d for v in self.vendors for d in v.documents}
        return names

    @property
    def document_names(self) -> list[str]:
        return sorted(p.name for p in (self.root / "documents").iterdir() if p.is_file())

    def subset(
        self, chat: int | None = None, findings: int | None = None, vendors: int | None = None
    ) -> "Dataset":
        """The first few examples of each kind (used by smoke tests on a micro dataset)."""
        return self.model_copy(
            update={
                "chat": self.chat[:chat] if chat is not None else self.chat,
                "findings": self.findings[:findings] if findings is not None else self.findings,
                "vendors": self.vendors[:vendors] if vendors is not None else self.vendors,
            }
        )


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_manifest(root: Path, manifest: Manifest) -> None:
    """Every listed file must exist with exactly the recorded hash, and no file may be unlisted."""
    problems: list[str] = []
    for relative, expected in manifest.files.items():
        path = root / relative
        if not path.is_file():
            problems.append(f"missing: {relative}")
        elif sha256_of(path) != expected:
            problems.append(f"changed: {relative}")
    on_disk = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "manifest.yaml"
    }
    problems += [f"not in the manifest: {name}" for name in sorted(on_disk - set(manifest.files))]
    if problems:
        raise DatasetError(
            f"dataset {manifest.version} does not match its manifest ({'; '.join(problems[:8])}). "
            "Regenerate it with `python -m eval.datasets.generate_v1` and commit the result."
        )


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def load_dataset(version: str = DEFAULT_VERSION, root: Path | None = None) -> Dataset:
    base = (root or DATASETS_ROOT) / version
    try:
        manifest = Manifest.model_validate(
            yaml.safe_load((base / "manifest.yaml").read_text(encoding="utf-8"))
        )
    except (OSError, yaml.YAMLError) as exc:
        raise DatasetError(f"cannot read the manifest of dataset {version}: {exc}") from exc
    verify_manifest(base, manifest)
    return Dataset(
        root=base,
        manifest=manifest,
        chat=[ChatExample.model_validate(r) for r in _jsonl(base / "chat_qa.jsonl")],
        findings=[
            FindingsExample.model_validate(r) for r in _jsonl(base / "contract_findings.jsonl")
        ],
        vendors=[VendorCase.model_validate(r) for r in _jsonl(base / "vendor_cases.jsonl")],
    )
