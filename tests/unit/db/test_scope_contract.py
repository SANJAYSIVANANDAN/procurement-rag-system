"""Every user-data repository method must take an OwnershipScope (spec 15.7)."""

import inspect

import pytest

from covenantiq.db.repositories import ALL_REPOSITORIES, Repository, UserRepository

USER_DATA_REPOSITORIES = [repo for repo in ALL_REPOSITORIES if repo.user_data]


def public_methods(repo: type[Repository]) -> list[tuple[str, object]]:
    return [
        (name, member)
        for name, member in inspect.getmembers(repo, inspect.isfunction)
        if not name.startswith("_")
    ]


@pytest.mark.parametrize("repo", USER_DATA_REPOSITORIES, ids=lambda repo: repo.__name__)
def test_methods_take_a_scope_or_are_system_methods(repo: type[Repository]) -> None:
    for name, method in public_methods(repo):
        if name.startswith("system_"):
            continue
        parameters = inspect.signature(method).parameters  # type: ignore[arg-type]
        assert "scope" in parameters, f"{repo.__name__}.{name} must take `scope` or be system_*"


@pytest.mark.parametrize("repo", USER_DATA_REPOSITORIES, ids=lambda repo: repo.__name__)
def test_system_methods_do_not_take_a_scope(repo: type[Repository]) -> None:
    for name, method in public_methods(repo):
        if name.startswith("system_"):
            assert "scope" not in inspect.signature(method).parameters  # type: ignore[arg-type]


def test_only_users_are_exempt() -> None:
    assert {repo for repo in ALL_REPOSITORIES if not repo.user_data} == {UserRepository}


def test_registry_lists_every_repository() -> None:
    assert set(Repository.__subclasses__()) == set(ALL_REPOSITORIES)
