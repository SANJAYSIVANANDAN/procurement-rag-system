"""The scripted "model" of the offline run (spec 28.5).

The offline evaluation drives the real ingestion, retrieval, evidence gate, graphs, validators and
scoring with model outputs that are fixed in advance, so it measures the deterministic logic and
nothing about any model. Two scripts:

* a reference answerer for chat: it writes the labelled ground-truth answer and cites the retrieved
  sources that contain the labelled passages (or the top source, if retrieval missed them). For a
  question that has no answer it plays the worst case, a confident claim that nothing supports, to
  test the safety net (evidence gate, citation validator) rather than a well-behaved model;
* an oracle for rule assessment: it answers each rule as a careful model would from the labels,
  *when the labelled clause is among the passages it was shown*; if retrieval did not surface it,
  it says the clause is not there, which is how a retrieval miss shows up in the findings.
"""

import re
from collections.abc import Mapping

from covenantiq.ai.schemas import RuleAssessment, RuleAssessmentBatch
from eval.dataset import ChatExample, ExpectedFinding
from eval.metrics.text import jaccard

_SOURCE = re.compile(r'<source id="C(\d+)"[^>]*>\n(.*?)\n</source>', re.S)
_RULE = re.compile(r'<rule id="([A-Z]+-\d{3})"[^>]*>(.*?)</rule>', re.S)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
FABRICATED = (
    "The Customer must give ninety (90) days' written notice and pay a termination fee of 25%."
)
STATUS = {"acceptable": "acceptable", "problematic": "problematic", "ambiguous": "ambiguous"}
RED_FLAGS = {"acceptable": [], "problematic": ["risk noted"], "ambiguous": ["unclear wording"]}
RELEVANT_OVERLAP = 0.5


def sources_of(prompt: str) -> list[tuple[int, str]]:
    return [(int(m.group(1)), m.group(2)) for m in _SOURCE.finditer(prompt)]


def _flat(text: str) -> str:
    return " ".join(text.split())


def contains_passage(source: str, passage: str) -> bool:
    """The source holds the labelled passage: verbatim, or with most of its words."""
    return _flat(passage) in _flat(source) or jaccard(source, passage) >= RELEVANT_OVERLAP


def scripted_answer(prompt: str, example: ChatExample) -> str:
    sources = sources_of(prompt)
    first = sources[0][0] if sources else 1
    if not example.answerable:
        return f"{FABRICATED.rstrip('.')} [C{first}]."
    wanted = [p.text for p in example.relevant_passages]
    markers = [m for m, text in sources if any(contains_passage(text, w) for w in wanted)] or [
        first
    ]
    tag = " [" + ", ".join(f"C{m}" for m in markers) + "]"
    sentences = [s for s in _SENTENCE.split(example.ground_truth_answer.strip()) if s]
    return " ".join(f"{s.rstrip('.!?')}{tag}." for s in sentences)


def oracle_assessments(prompt: str, expected: Mapping[str, ExpectedFinding]) -> RuleAssessmentBatch:
    assessments: list[RuleAssessment] = []
    for match in _RULE.finditer(prompt):
        rule_id, block = match.group(1), match.group(2)
        label = expected.get(rule_id)
        hit: tuple[int, str] | None = None
        if label is not None and label.status in STATUS:
            wanted = [p.text for p in label.evidence]
            hit = next(
                (
                    (m, t)
                    for m, t in sources_of(block)
                    if any(contains_passage(t, w) for w in wanted)
                ),
                None,
            )
        if label is not None and hit is not None:
            assessments.append(
                RuleAssessment(
                    rule_id=rule_id,
                    clause_found="yes",
                    supporting_markers=[hit[0]],
                    status=STATUS[label.status],
                    red_flags=RED_FLAGS[label.status],
                    explanation=hit[1][:1000],
                    business_impact="",
                    recommended_action="",
                    confidence="high",
                )
            )
        else:
            assessments.append(
                RuleAssessment(
                    rule_id=rule_id,
                    clause_found="no",
                    supporting_markers=[],
                    status="absent",
                    explanation="No passage shown is this clause.",
                    confidence="high",
                )
            )
    return RuleAssessmentBatch(assessments=assessments)
