"""Loading the ruleset (spec 20.3). A bad file is an error at start-up, never a thinner analysis."""

from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from covenantiq.analysis.contract.rules_schema import RulesFile, RuleSpec, UnusualProvision
from covenantiq.core.enums import ContractType

DEFAULT_RULES_PATH = Path("config/rules/contract_baseline_v1.yaml")
DEFAULT_TAXONOMY_PATH = Path("config/clause_taxonomy.yaml")


class RulesetError(ValueError):
    """The rules file is unreadable or invalid. The message says which rule and why."""


class Ruleset:
    def __init__(self, file: RulesFile) -> None:
        self.file = file
        self._by_id = {rule.id: rule for rule in file.rules}

    @property
    def version(self) -> str:
        return self.file.version

    @property
    def unusual(self) -> list[UnusualProvision]:
        return list(self.file.unusual)

    def rule(self, rule_id: str) -> RuleSpec:
        return self._by_id[rule_id]

    def applicable(self, contract_type: ContractType) -> list[RuleSpec]:
        return [rule for rule in self.file.rules if rule.applies(contract_type)]


def taxonomy_types(path: Path = DEFAULT_TAXONOMY_PATH) -> set[str]:
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return set((raw.get("types") or {}).keys())


def check_against_taxonomy(file: RulesFile, known: Iterable[str]) -> None:
    """Every `clause_types` entry must be a type the chunker can actually assign."""
    known_set = set(known)
    problems = [
        f"{rule.id}: unknown clause type '{name}'"
        for rule in file.rules
        for name in rule.clause_types
        if name not in known_set
    ]
    if problems:
        raise RulesetError("; ".join(problems))


def load_ruleset(path: Path | None = None, taxonomy_path: Path | None = None) -> Ruleset:
    target = path or DEFAULT_RULES_PATH
    try:
        with target.open(encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise RulesetError(f"cannot read the rules file {target}: {exc}") from exc
    try:
        file = RulesFile.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8]
        )
        raise RulesetError(f"invalid rules file {target}: {details}") from exc
    check_against_taxonomy(file, taxonomy_types(taxonomy_path or DEFAULT_TAXONOMY_PATH))
    return Ruleset(file)


@lru_cache(maxsize=1)
def get_ruleset() -> Ruleset:
    return load_ruleset()
