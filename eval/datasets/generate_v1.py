"""Generates evaluation dataset v1.0.0: synthetic contracts and the labels that go with them.

Every contract is assembled from a clause library with controlled variants for each topic: a good
clause, a problematic one, an ambiguous one, or none at all. Because each label is derived from the
same library entry that produced the clause text, the labels are right by construction, and a
contract that lacks a clause really lacks it. The output is committed; this script is the record of
how it was made. It is deterministic: running it again changes nothing.

    uv run python -m eval.datasets.generate_v1

The contracts are written for this project and carry no third-party text or licence. Importing an
annotated public subset (for example CUAD, CC BY 4.0) is left for a later dataset version.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from covenantiq.analysis.contract.rules_loader import load_ruleset
from covenantiq.core.enums import ContractType

ROOT = Path(__file__).parent / "v1.0.0"
VERSION = "1.0.0"
SECTIONS_PER_PAGE = 3

RULESET = load_ruleset()

PRESENT = {"good": "acceptable", "problematic": "problematic", "ambiguous": "ambiguous"}


@dataclass(frozen=True)
class Variant:
    paragraphs: tuple[str, ...]
    fact: str  # the answer to the topic's question, in words the clause itself uses


@dataclass(frozen=True)
class Topic:
    id: str
    rule_id: str
    title: str
    question: str  # `{v}` is the vendor's name
    variants: dict[str, Variant]


TOPICS: tuple[Topic, ...] = (
    Topic(
        "liability",
        "LIAB-001",
        "Limitation of Liability",
        "How is {v}'s liability limited in this agreement?",
        {
            "good": Variant(
                (
                    "The total aggregate liability of the Supplier arising out of or in connection "
                    "with this Agreement shall not exceed the fees paid or payable by the Customer "
                    "in the twelve (12) months preceding the event giving rise to the claim.",
                    "The limitation in the previous clause shall not apply to liability for fraud "
                    "or for death or personal injury caused by negligence.",
                ),
                "Liability is capped at the fees paid or payable in the twelve (12) months "
                "preceding the claim, and the cap does not apply to fraud or to death or "
                "personal injury caused by negligence.",
            ),
            "problematic": Variant(
                (
                    "The Customer accepts unlimited liability for any loss or damage arising from "
                    "its use of the Services, and no cap applies to claims made against it.",
                ),
                "The Customer accepts unlimited liability for loss or damage arising from its "
                "use of the Services, with no cap.",
            ),
            "ambiguous": Variant(
                (
                    "The liability of each party under this Agreement shall be limited to such "
                    "amount as is reasonable in all the circumstances.",
                ),
                "Liability is limited to such amount as is reasonable in all the circumstances, "
                "and no figure is stated.",
            ),
        },
    ),
    Topic(
        "consequential",
        "LIAB-002",
        "Exclusion of Consequential Loss",
        "Does this agreement exclude liability for indirect or consequential loss?",
        {
            "good": Variant(
                (
                    "Neither party shall be liable to the other for any indirect, consequential, "
                    "special or incidental loss, including loss of profits, arising under this "
                    "Agreement.",
                ),
                "Neither party is liable for indirect, consequential, special or incidental "
                "loss, including loss of profits.",
            )
        },
    ),
    Topic(
        "indemnity",
        "IND-001",
        "Indemnification",
        "Who indemnifies whom under the {v} agreement?",
        {
            "good": Variant(
                (
                    "Each party shall defend and indemnify the other against third-party claims "
                    "arising from its breach of this Agreement.",
                ),
                "Each party defends and indemnifies the other against third-party claims "
                "arising from its breach of this Agreement.",
            )
        },
    ),
    Topic(
        "term_convenience",
        "TERM-001",
        "Termination for Convenience",
        "Can the customer terminate the {v} agreement for convenience, and on what notice?",
        {
            "good": Variant(
                (
                    "The Customer may terminate this Agreement for convenience at any time on "
                    "thirty (30) days' written notice to the Supplier.",
                ),
                "The Customer may terminate for convenience at any time on thirty (30) days' "
                "written notice to the Supplier.",
            )
        },
    ),
    Topic(
        "termination_cause",
        "TERM-002",
        "Termination for Material Breach",
        "When can either party terminate the {v} agreement for material breach?",
        {
            "good": Variant(
                (
                    "Either party may terminate this Agreement with immediate effect by written "
                    "notice if the other party commits a material breach and fails to remedy it "
                    "within thirty (30) days of being notified.",
                ),
                "Either party may terminate immediately by written notice if the other commits "
                "a material breach and fails to remedy it within thirty (30) days of notice.",
            ),
            "ambiguous": Variant(
                (
                    "Either party may terminate this Agreement for material breach if the breach "
                    "is not remedied within a reasonable period.",
                ),
                "Either party may terminate for material breach if it is not remedied within a "
                "reasonable period, which is not defined.",
            ),
        },
    ),
    Topic(
        "renewal",
        "REN-001",
        "Term and Renewal",
        "Does the {v} agreement renew automatically, and how is renewal handled?",
        {
            "good": Variant(
                (
                    "This Agreement continues for an initial term of twelve (12) months. It does "
                    "not renew unless both parties agree in writing.",
                ),
                "The Agreement runs for an initial term of twelve (12) months and renews only "
                "if both parties agree in writing.",
            ),
            "problematic": Variant(
                (
                    "This Agreement shall automatically renew for successive terms of twelve "
                    "(12) months unless the Customer gives notice of non-renewal at least ninety "
                    "(90) days before the end of the then-current term.",
                ),
                "The Agreement automatically renews for successive twelve (12) month terms "
                "unless the Customer gives notice of non-renewal at least ninety (90) days "
                "before the end of the term.",
            ),
        },
    ),
    Topic(
        "payment",
        "PAY-001",
        "Fees and Payment",
        "When must the customer pay {v}'s invoices?",
        {
            "good": Variant(
                (
                    "The Customer shall pay each undisputed invoice within thirty (30) days of "
                    "receipt, in United States dollars.",
                ),
                "Each undisputed invoice must be paid within thirty (30) days of receipt, in "
                "United States dollars.",
            ),
            "ambiguous": Variant(
                ("The Customer shall pay invoices promptly after receipt.",),
                "Invoices must be paid promptly after receipt, with no number of days stated.",
            ),
        },
    ),
    Topic(
        "price",
        "PAY-002",
        "Price Changes",
        "Can {v} change its fees during the term?",
        {
            "good": Variant(
                (
                    "Fees are fixed for the initial term. Any increase on renewal shall not "
                    "exceed three per cent (3%) and requires ninety (90) days' prior written "
                    "notice.",
                ),
                "Fees are fixed for the initial term, and a renewal increase may not exceed "
                "three per cent (3%) and needs ninety (90) days' prior written notice.",
            ),
            "problematic": Variant(
                ("The Supplier may increase the fees at any time upon notice to the Customer.",),
                "The Supplier may increase the fees at any time upon notice to the Customer.",
            ),
        },
    ),
    Topic(
        "confidentiality",
        "CONF-001",
        "Confidentiality",
        "What confidentiality obligations apply under the {v} agreement?",
        {
            "good": Variant(
                (
                    "Each party shall keep the other party's Confidential Information confidential "
                    "and shall not disclose it to any third party for five (5) years after "
                    "disclosure, except as required by law.",
                ),
                "Each party must keep the other's Confidential Information confidential and not "
                "disclose it to any third party for five (5) years, except as required by law.",
            )
        },
    ),
    Topic(
        "data_protection",
        "DP-001",
        "Data Protection",
        "What data protection obligations apply under the {v} agreement?",
        {
            "good": Variant(
                (
                    "Each party shall comply with applicable data protection law. The Supplier "
                    "shall process personal data only on the documented instructions of the "
                    "Customer.",
                ),
                "Each party must comply with applicable data protection law, and the Supplier "
                "may process personal data only on the Customer's documented instructions.",
            )
        },
    ),
    Topic(
        "dp_breach",
        "DP-002",
        "Breach Notification",
        "How quickly must {v} notify the customer of a personal data breach?",
        {
            "good": Variant(
                (
                    "The Supplier shall notify the Customer of any personal data breach without "
                    "undue delay and in any event within seventy-two (72) hours of becoming "
                    "aware of it.",
                ),
                "The Supplier must notify the Customer of a personal data breach without undue "
                "delay and within seventy-two (72) hours of becoming aware of it.",
            ),
            "ambiguous": Variant(
                ("The Supplier shall notify the Customer of a personal data breach promptly.",),
                "The Supplier must notify the Customer of a personal data breach promptly, with "
                "no time limit stated.",
            ),
        },
    ),
    Topic(
        "subprocessors",
        "DP-003",
        "Sub-processors",
        "What notice must {v} give before appointing a sub-processor?",
        {
            "good": Variant(
                (
                    "The Supplier shall give the Customer thirty (30) days' notice of any new "
                    "sub-processor, and the Customer may object to the appointment.",
                ),
                "The Supplier must give thirty (30) days' notice of any new sub-processor, and "
                "the Customer may object.",
            )
        },
    ),
    Topic(
        "sla",
        "SLA-001",
        "Service Levels",
        "What availability does {v} commit to for the service?",
        {
            "good": Variant(
                (
                    "The Supplier shall make the Services available at least 99.9% of the time in "
                    "each calendar month, excluding scheduled maintenance.",
                ),
                "The Services must be available at least 99.9% of the time in each calendar "
                "month, excluding scheduled maintenance.",
            )
        },
    ),
    Topic(
        "governing_law",
        "GOV-001",
        "Governing Law",
        "Which law governs the {v} agreement?",
        {
            "good": Variant(
                (
                    "This Agreement is governed by the laws of England and Wales, and the courts "
                    "of England and Wales have exclusive jurisdiction.",
                ),
                "The Agreement is governed by the laws of England and Wales, and its courts have "
                "exclusive jurisdiction.",
            )
        },
    ),
    Topic(
        "customer_data",
        "IP-002",
        "Customer Data",
        "Who owns the customer data and what may {v} do with it?",
        {
            "good": Variant(
                (
                    "All Customer Data remains the exclusive property of the Customer. The "
                    "Supplier shall use Customer Data only to provide the Services.",
                ),
                "Customer Data remains the exclusive property of the Customer, and the Supplier "
                "may use it only to provide the Services.",
            ),
            "problematic": Variant(
                (
                    "The Supplier may use Customer Data for its own purposes, including to "
                    "improve its products and services.",
                ),
                "The Supplier may use Customer Data for its own purposes, including to improve "
                "its products and services.",
            ),
        },
    ),
    Topic(
        "audit",
        "AUD-001",
        "Audit Rights",
        "Can the customer audit {v}?",
        {
            "good": Variant(
                (
                    "The Customer may, on reasonable notice and not more than once a year, audit "
                    "the Supplier's records relating to the Services.",
                ),
                "The Customer may audit the Supplier's records relating to the Services on "
                "reasonable notice, not more than once a year.",
            )
        },
    ),
    Topic(
        "force_majeure",
        "FM-001",
        "Force Majeure",
        "What happens if an event beyond a party's control prevents performance under the "
        "{v} agreement?",
        {
            "good": Variant(
                (
                    "Neither party is liable for delay caused by events beyond its reasonable "
                    "control, and either party may terminate if such an event continues for more "
                    "than sixty (60) days.",
                ),
                "Neither party is liable for delay caused by events beyond its reasonable "
                "control, and either may terminate if the event lasts more than sixty (60) days.",
            )
        },
    ),
)
TOPIC = {t.id: t for t in TOPICS}


@dataclass(frozen=True)
class DocSpec:
    name: str
    contract_type: ContractType
    vendor: str
    title: str
    variants: dict[str, str]  # topic id -> good | problematic | ambiguous (absent if missing)


G, P, A = "good", "problematic", "ambiguous"
DOCS: tuple[DocSpec, ...] = (
    DocSpec(
        "acme_saas",
        ContractType.SAAS,
        "Acme",
        "Acme Cloud Subscription Agreement",
        dict(
            liability=G,
            consequential=G,
            indemnity=G,
            term_convenience=G,
            termination_cause=G,
            renewal=G,
            payment=G,
            price=G,
            confidentiality=G,
            data_protection=G,
            dp_breach=G,
            subprocessors=G,
            sla=G,
            governing_law=G,
            customer_data=G,
            audit=G,
            force_majeure=G,
        ),
    ),
    DocSpec(
        "acme_dpa",
        ContractType.DPA,
        "Acme",
        "Acme Data Processing Agreement",
        dict(
            liability=G,
            termination_cause=G,
            payment=G,
            confidentiality=G,
            data_protection=G,
            dp_breach=G,
            subprocessors=G,
            audit=G,
            governing_law=G,
            force_majeure=G,
        ),
    ),
    DocSpec(
        "globex_saas",
        ContractType.SAAS,
        "Globex",
        "Globex Platform Services Agreement",
        dict(
            liability=P,
            termination_cause=A,
            renewal=P,
            payment=A,
            price=P,
            confidentiality=G,
            governing_law=G,
            customer_data=P,
            force_majeure=G,
        ),
    ),
    DocSpec(
        "globex_dpa",
        ContractType.DPA,
        "Globex",
        "Globex Data Processing Addendum",
        dict(
            liability=A,
            termination_cause=A,
            payment=G,
            confidentiality=G,
            data_protection=G,
            dp_breach=A,
            governing_law=G,
        ),
    ),
    DocSpec(
        "initech_msa",
        ContractType.MSA,
        "Initech",
        "Initech Master Services Agreement",
        dict(
            liability=G,
            consequential=G,
            indemnity=G,
            term_convenience=G,
            termination_cause=G,
            renewal=P,
            payment=G,
            price=G,
            confidentiality=G,
            governing_law=G,
            force_majeure=G,
        ),
    ),
    DocSpec(
        "hooli_services",
        ContractType.SERVICES,
        "Hooli",
        "Hooli Professional Services Agreement",
        dict(
            liability=A,
            consequential=G,
            term_convenience=G,
            termination_cause=G,
            payment=A,
            confidentiality=G,
            data_protection=G,
            dp_breach=G,
            customer_data=G,
            audit=G,
            governing_law=G,
        ),
    ),
    DocSpec(
        "umbrella_nda",
        ContractType.NDA,
        "Umbrella",
        "Umbrella Mutual Non-Disclosure Agreement",
        dict(liability=G, termination_cause=G, confidentiality=G, governing_law=G),
    ),
    DocSpec(
        "stark_saas",
        ContractType.SAAS,
        "Stark",
        "Stark Industries Software Subscription",
        dict(
            liability=G,
            consequential=G,
            indemnity=G,
            term_convenience=G,
            termination_cause=G,
            renewal=G,
            payment=G,
            price=P,
            confidentiality=G,
            data_protection=G,
            dp_breach=A,
            subprocessors=G,
            sla=G,
            governing_law=G,
            customer_data=G,
            force_majeure=G,
        ),
    ),
    DocSpec(
        "wayne_license",
        ContractType.LICENSE,
        "Wayne",
        "Wayne Enterprises Software Licence",
        dict(
            liability=G,
            indemnity=G,
            termination_cause=G,
            renewal=P,
            payment=G,
            price=G,
            confidentiality=G,
            governing_law=G,
            force_majeure=G,
        ),
    ),
    DocSpec(
        "wonka_supply",
        ContractType.SUPPLY,
        "Wonka",
        "Wonka Supply Agreement",
        dict(
            liability=P,
            consequential=G,
            termination_cause=A,
            payment=G,
            governing_law=G,
            force_majeure=G,
        ),
    ),
    DocSpec(
        "piedpiper_saas",
        ContractType.SAAS,
        "Pied Piper",
        "Pied Piper Hosted Service Terms",
        dict(
            liability=G,
            indemnity=G,
            termination_cause=G,
            renewal=P,
            payment=G,
            price=G,
            confidentiality=G,
            data_protection=G,
            dp_breach=G,
            sla=G,
            customer_data=G,
            audit=G,
            force_majeure=G,
        ),
    ),
    DocSpec(
        "soylent_services",
        ContractType.SERVICES,
        "Soylent",
        "Soylent Consulting Services Agreement",
        dict(
            liability=G,
            indemnity=G,
            termination_cause=A,
            payment=G,
            confidentiality=G,
            audit=G,
            governing_law=G,
            force_majeure=G,
        ),
    ),
)

VENDORS = (
    ("Acme", ("acme_saas", "acme_dpa")),
    ("Globex", ("globex_saas", "globex_dpa")),
    ("Initech", ("initech_msa",)),
    ("Hooli", ("hooli_services",)),
    ("Stark", ("stark_saas",)),
)
DIMENSION_CLAIMS = {
    "Globex": {"legal": "higher_risk_than:Acme", "data_security": "higher_risk_than:Acme"},
    "Stark": {"financial_commercial": "higher_risk_than:Acme"},
}


@dataclass
class Section:
    topic: Topic
    variant: str
    number: int
    page: int
    text: str
    refs: list[str] = field(default_factory=list)


def build_sections(doc: DocSpec) -> list[Section]:
    """The doc's sections in order, numbered from 2 (1 is the parties clause)."""
    sections: list[Section] = []
    for topic in TOPICS:
        key = doc.variants.get(topic.id)
        if key is None:
            continue
        number = len(sections) + 2
        variant = topic.variants[key]
        refs = [f"{number}.{i}" for i in range(1, len(variant.paragraphs) + 1)]
        body = " ".join(f"{ref} {text}" for ref, text in zip(refs, variant.paragraphs, strict=True))
        sections.append(
            Section(topic, key, number, 1 + (number - 2) // SECTIONS_PER_PAGE, body, refs)
        )
    return sections


def render_document(doc: DocSpec, sections: list[Section]) -> str:
    """The contract as the text extractor reads it: `@page N` lines, `# ` headings, paragraphs."""
    lines = [
        "@page 1",
        f"# {doc.title}",
        "# 1. Parties and Interpretation",
        f"1.1 This {doc.contract_type.value} agreement is made between {doc.vendor} (the "
        f'"Supplier") and the customer named in the order form (the "Customer").',
        "",
    ]
    page = 1
    for section in sections:
        if section.page != page:
            page = section.page
            lines += [f"@page {page}", ""]
        lines.append(f"# {section.number}. {section.topic.title}")
        lines.extend(
            f"{ref} {text}"
            for ref, text in zip(
                section.refs, section.topic.variants[section.variant].paragraphs, strict=True
            )
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def passage(doc: DocSpec, section: Section) -> dict[str, Any]:
    return {
        "document": f"{doc.name}.txt",
        "page": section.page,
        "clause_ref": section.refs[0],
        "text": section.text,
    }


def expected_findings(doc: DocSpec, sections: list[Section]) -> list[dict[str, Any]]:
    by_rule = {s.topic.rule_id: s for s in sections}
    findings: list[dict[str, Any]] = []
    for rule in RULESET.applicable(doc.contract_type):
        section = by_rule.get(rule.id)
        if section is None:
            findings.append({"rule_id": rule.id, "status": "absent"})
        else:
            findings.append(
                {
                    "rule_id": rule.id,
                    "status": PRESENT[section.variant],
                    "evidence": [passage(doc, section)],
                }
            )
    return findings


# ---- chat QA ------------------------------------------------------------------------------------


def chat_examples(built: dict[str, tuple[DocSpec, list[Section]]]) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []

    def add(**fields: Any) -> None:
        examples.append({"id": f"qa-{len(examples) + 1:04d}", **fields})

    for index, (name, (doc, sections)) in enumerate(built.items()):
        present = [s for s in sections]
        rotated = present[index % max(1, len(present)) :] + present[: index % max(1, len(present))]
        for section in rotated[:4]:
            variant = section.topic.variants[section.variant]
            add(
                document_refs=[f"{name}.txt"],
                question=section.topic.question.format(v=doc.vendor),
                conversation_history=[],
                answerable=True,
                ground_truth_answer=variant.fact,
                relevant_passages=[passage(doc, section)],
                expected_citations=[
                    {k: v for k, v in passage(doc, section).items() if k != "text"}
                ],
                tags=[section.topic.id, "single_doc", section.variant],
            )
        absent = [
            t
            for t in TOPICS
            if t.id not in doc.variants
            and any(r.id == t.rule_id for r in RULESET.applicable(doc.contract_type))
        ]
        for topic in absent[:2]:
            add(
                document_refs=[f"{name}.txt"],
                question=topic.question.format(v=doc.vendor),
                conversation_history=[],
                answerable=False,
                ground_truth_answer="",
                relevant_passages=[],
                expected_citations=[],
                tags=[topic.id, "single_doc", "unanswerable"],
            )

    def section_of(doc_name: str, topic_id: str) -> tuple[DocSpec, Section]:
        doc, sections = built[doc_name]
        return doc, next(s for s in sections if s.topic.id == topic_id)

    comparisons = [
        (
            "price",
            "acme_saas",
            "stark_saas",
            "Compare how the Acme and Stark agreements treat fee increases.",
        ),
        (
            "liability",
            "acme_saas",
            "globex_saas",
            "Compare how liability is limited in the Acme and Globex SaaS agreements.",
        ),
        (
            "payment",
            "acme_dpa",
            "globex_dpa",
            "Compare the payment terms in the Acme and Globex data processing agreements.",
        ),
        (
            "dp_breach",
            "acme_saas",
            "stark_saas",
            "Compare how quickly Acme and Stark must notify a personal data breach.",
        ),
    ]
    for topic_id, first, second, question in comparisons:
        (d1, s1), (d2, s2) = section_of(first, topic_id), section_of(second, topic_id)
        add(
            document_refs=[f"{first}.txt", f"{second}.txt"],
            question=question,
            conversation_history=[],
            answerable=True,
            ground_truth_answer=(
                f"{d1.vendor}: {s1.topic.variants[s1.variant].fact} "
                f"{d2.vendor}: {s2.topic.variants[s2.variant].fact}"
            ),
            relevant_passages=[passage(d1, s1), passage(d2, s2)],
            expected_citations=[
                {k: v for k, v in passage(d, s).items() if k != "text"}
                for d, s in ((d1, s1), (d2, s2))
            ],
            tags=[topic_id, "multi_doc"],
        )

    follow_ups = [
        (
            "acme_saas",
            "term_convenience",
            "Can the customer end the Acme agreement early?",
            "And on how much notice?",
            "How much notice must the customer give to terminate the Acme agreement for convenience?",
        ),
        (
            "globex_saas",
            "renewal",
            "Does the Globex agreement renew automatically?",
            "How long before the end must notice be given?",
            "How long before the end of the term must the customer give notice of non-renewal under the Globex agreement?",
        ),
        (
            "stark_saas",
            "price",
            "Can Stark change its fees?",
            "Is there a limit on that?",
            "Is there a limit on how much Stark may increase its fees?",
        ),
        (
            "acme_saas",
            "dp_breach",
            "Does the Acme agreement cover personal data breaches?",
            "What is the deadline?",
            "What is the deadline for Acme to notify the customer of a personal data breach?",
        ),
        (
            "initech_msa",
            "payment",
            "Who pays under the Initech agreement?",
            "Within how many days?",
            "Within how many days must the customer pay Initech's invoices?",
        ),
        (
            "wayne_license",
            "renewal",
            "Is the Wayne licence renewed automatically?",
            "How do I stop that?",
            "How can the customer stop the Wayne licence from renewing automatically?",
        ),
    ]
    for name, topic_id, first_question, follow_up, standalone in follow_ups:
        doc, section = section_of(name, topic_id)
        fact = section.topic.variants[section.variant].fact
        add(
            document_refs=[f"{name}.txt"],
            question=follow_up,
            standalone_question=standalone,
            conversation_history=[
                {"role": "user", "content": first_question},
                {"role": "assistant", "content": fact},
            ],
            answerable=True,
            ground_truth_answer=fact,
            relevant_passages=[passage(doc, section)],
            expected_citations=[{k: v for k, v in passage(doc, section).items() if k != "text"}],
            tags=[topic_id, "follow_up", section.variant],
        )
    return examples


# ---- vendors ------------------------------------------------------------------------------------

_RISK = {"favorable": 0, "ambiguous": 1, "unfavorable": 2}


def _signals_by_rule() -> dict[str, list[str]]:
    raw = yaml.safe_load(Path("config/vendor/signals_v1.yaml").read_text(encoding="utf-8"))
    return {
        s["id"]: s["source"]["contract_rules"]
        for s in raw["signals"]
        if s["source"].get("contract_rules")
    }


def vendor_cases(built: dict[str, tuple[DocSpec, list[Section]]]) -> list[dict[str, Any]]:
    """Expected signal statuses, worked out from the labels with a small independent
    implementation of the aggregation rules (not the pipeline's own code)."""
    sources = _signals_by_rule()
    cases = []
    for index, (vendor, documents) in enumerate(VENDORS, start=1):
        per_signal: dict[str, str] = {}
        for signal, rules in sources.items():
            readings: list[str] = []
            absent_everywhere = True
            for name in documents:
                doc, _ = built[name]
                labels = {f["rule_id"]: f["status"] for f in expected_findings(doc, built[name][1])}
                doc_readings = [
                    {
                        "acceptable": "favorable",
                        "problematic": "unfavorable",
                        "ambiguous": "ambiguous",
                    }[labels[r]]
                    for r in rules
                    if r in labels and labels[r] in ("acceptable", "problematic", "ambiguous")
                ]
                if doc_readings:
                    readings.append(max(doc_readings, key=_RISK.__getitem__))
                    absent_everywhere = False
                elif not any(r in labels and labels[r] == "absent" for r in rules):
                    absent_everywhere = False
            if readings:
                per_signal[signal] = max(readings, key=_RISK.__getitem__)
            elif absent_everywhere:
                per_signal[signal] = "absent"
            else:
                per_signal[signal] = "insufficient_evidence"
        cases.append(
            {
                "id": f"vc-{index:04d}",
                "vendor": vendor,
                "contract_type_note": "contract-sourced signals only: extraction-sourced signals need a real model",
                "documents": [f"{n}.txt" for n in documents],
                "expected_signals": per_signal,
                "expected_dimension_order": DIMENSION_CLAIMS.get(vendor, {}),
            }
        )
    return cases


# ---- writing ------------------------------------------------------------------------------------


def dump_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    (ROOT / "documents").mkdir(parents=True, exist_ok=True)
    built: dict[str, tuple[DocSpec, list[Section]]] = {}
    findings_rows: list[dict[str, Any]] = []
    for doc in DOCS:
        sections = build_sections(doc)
        built[doc.name] = (doc, sections)
        (ROOT / "documents" / f"{doc.name}.txt").write_text(
            render_document(doc, sections), encoding="utf-8", newline="\n"
        )
        expected = expected_findings(doc, sections)
        findings_rows.append(
            {
                "id": f"cf-{len(findings_rows) + 1:04d}",
                "document": f"{doc.name}.txt",
                "contract_type": doc.contract_type.value,
                "vendor": doc.vendor,
                "expected_findings": expected,
                "not_expected": [
                    f"{f['rule_id']}:acceptable" for f in expected if f["status"] == "absent"
                ],
            }
        )
    dump_jsonl(ROOT / "chat_qa.jsonl", chat_examples(built))
    dump_jsonl(ROOT / "contract_findings.jsonl", findings_rows)
    dump_jsonl(ROOT / "vendor_cases.jsonl", vendor_cases(built))

    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and p.name != "manifest.yaml")
    manifest: dict[str, Any] = {
        "version": VERSION,
        "schema_version": 1,
        "created": "2026-11-14",
        "description": (
            "Synthetic contracts assembled from a clause library with controlled presence, "
            "absence and ambiguity of clauses, with chat questions, expected contract findings "
            "and vendor expectations derived from the same library."
        ),
        "sources": ["synthetic: written for this project (eval/datasets/generate_v1.py)"],
        "licenses": ["Project-owned synthetic text. No third-party data is included."],
        "files": {p.relative_to(ROOT).as_posix(): sha256(p) for p in files},
    }
    (ROOT / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {len(files)} files to {ROOT}")


if __name__ == "__main__":
    main()
