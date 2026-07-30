"""Custom ansible-lint rule: become keys come immediately after name.

Convention: within a task, `become` and its `become_*` companions sort directly
after `name`, ahead of the module and every other key. This is a *chosen*
convention, not one a codebase follows by default (it was a ~24% minority in the
repo that adopted it), so it is enforced by tooling rather than left to habit.

Autofix-capable (TransformMixin): `ansible-lint --fix` hoists the become group.
Modeled on ansiblelint.rules.key_order.KeyOrderRule.
"""

from __future__ import annotations

import functools
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ansiblelint.errors import MatchError, RuleMatchTransformMeta
from ansiblelint.rules import AnsibleLintRule, TransformMixin

if TYPE_CHECKING:
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    from ansiblelint.file_utils import Lintable
    from ansiblelint.utils import Task

# name first, then the become-* group, then everything else. `None` is the
# catch-all bucket (the module action + all other keys) — its members keep their
# existing relative order (stable sort), so only the become group is hoisted.
SORTER: tuple[str | None, ...] = (
    "name",
    "become",
    "become_user",
    "become_method",
    "become_flags",
    "become_exe",
    None,
)


def _sort_index(name: str) -> int:
    catch = -1
    for i, value in enumerate(SORTER):
        if value == name:
            return i
        if value is None:
            catch = i
    return catch


def _cmp(prop1: str, prop2: str) -> int:
    v1, v2 = _sort_index(prop1), _sort_index(prop2)
    return (v1 > v2) - (v1 < v2)


@dataclass(frozen=True)
class BecomeFirstTMeta(RuleMatchTransformMeta):
    """Transform metadata carrying the corrected key order."""

    fixed: tuple[str, ...]

    def __str__(self) -> str:  # pragma: no cover
        return f"Fixed to {self.fixed}"


class BecomeFirstRule(AnsibleLintRule, TransformMixin):
    """become and its become_* keys must come directly after name."""

    id = "become-first"
    severity = "LOW"
    tags = ["formatting"]
    version_changed = "1.0.0"
    needs_raw_task = True
    _ids = {
        "become-first[task]": "become (and become_*) should come right after name",
    }

    def matchtask(
        self,
        task: Task,
        file: Lintable | None = None,
    ) -> list[MatchError]:
        raw_task = task["__raw_task__"]
        keys = [str(key) for key in raw_task if not str(key).startswith("_")]
        if "become" not in keys:
            return []
        sorted_keys = sorted(keys, key=functools.cmp_to_key(_cmp))
        if keys == sorted_keys:
            return []
        return [
            self.create_matcherror(
                "become and its become_* keys should come right after name: "
                f"{', '.join(sorted_keys)}",
                filename=file,
                tag="become-first[task]",
                transform_meta=BecomeFirstTMeta(fixed=tuple(sorted_keys)),
            ),
        ]

    def transform(
        self,
        match: MatchError,
        lintable: Lintable,
        data: CommentedMap | CommentedSeq | str,
    ) -> None:
        if not isinstance(match.transform_meta, BecomeFirstTMeta):  # pragma: no cover
            return
        task = self.seek(match.yaml_path, data)
        for key in match.transform_meta.fixed:
            # another transform might already have removed the key
            if key in task:
                task[key] = task.pop(key)
        match.fixed = True


# Loaded only under pytest — smoke test the sorter.
if "pytest" in sys.modules:  # pragma: no cover
    import pytest

    @pytest.mark.parametrize(
        ("keys", "expected"),
        (
            (["name", "ansible.builtin.command", "become"],
             ["name", "become", "ansible.builtin.command"]),
            (["name", "become", "ansible.builtin.command"],
             ["name", "become", "ansible.builtin.command"]),
            (["ansible.builtin.command", "register", "become", "become_user"],
             ["become", "become_user", "ansible.builtin.command", "register"]),
        ),
    )
    def test_become_first_sorter(keys: list[str], expected: list[str]) -> None:
        assert sorted(keys, key=functools.cmp_to_key(_cmp)) == expected
