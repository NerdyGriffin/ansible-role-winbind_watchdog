"""Custom ansible-lint rule: no per-host os_* keys in inventory files.

Convention: a host entry must not declare `os_family`, `os_distribution`,
`os_version` or `os_edition`. For any host that can run the `setup` module these
are hand-maintained duplicates of gathered facts — they drift silently and
nothing reads them. Code that branches on OS reads `ansible_facts['os_family']`;
provisioning code that runs before the guest exists (a cloud-init template, say)
branches on inventory GROUP membership instead.

Scope is deliberately narrow: only keys set directly on a host entry underneath
a `hosts:` mapping in an inventory file (kind == "inventory"). It does NOT flag
the same names nested inside a structured var, because those are a different
namespace and legitimate — e.g. `hyperv_guest_config.base_images.<name>.
os_version`, which drives AVMA product-key resolution. Nor does it look at
group_vars/host_vars files (kind == "vars") — widening it there is a separate
call.

Escape hatch for a genuinely fact-less device (a network switch, a UPS card):
put the OS in a comment, or `# noqa: no-host-os-vars` if it must be a var.

Not autofix-capable: deleting a var is not a safe mechanical transform, since a
host that truly cannot gather facts may have nothing else recording its OS.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from ansiblelint.errors import MatchError
from ansiblelint.rules import AnsibleLintRule
from ansiblelint.skip_utils import get_rule_skips_from_line

if TYPE_CHECKING:
    from ansiblelint.file_utils import Lintable

# The per-host OS metadata keys this rule forbids.
BANNED_KEYS: frozenset[str] = frozenset(
    {"os_family", "os_distribution", "os_version", "os_edition"},
)


def find_host_os_vars(data: Any) -> list[tuple[str, str, int]]:
    """Return (hostname, key, line) for every banned key on a host entry.

    Follows the inventory group structure *strictly* — a group node's `hosts:`
    and `children:` only. It deliberately does NOT recurse into arbitrary
    mappings: a `vars:` value is free-form user data that may itself contain a
    key called `hosts`, and treating that as an inventory host entry would be a
    false positive.

    `line` is 1-based, from ruamel round-trip position data when available.
    """
    found: list[tuple[str, str, int]] = []

    def line_of(node: Any, key: str) -> int:
        lc = getattr(node, "lc", None)
        if lc is None or not hasattr(lc, "data"):
            return 0
        pos = lc.data.get(key)
        return pos[0] + 1 if pos else 0

    def walk_group(group: Any) -> None:
        """Visit one group node: its `hosts:`, then its `children:` groups."""
        if not isinstance(group, dict):
            return

        hosts = group.get("hosts")
        if isinstance(hosts, dict):
            for hostname, host_vars in hosts.items():
                # `hosts:` entries are commonly null (`hostname:` alone).
                if not isinstance(host_vars, dict):
                    continue
                for banned in sorted(BANNED_KEYS.intersection(host_vars)):
                    found.append(
                        (str(hostname), banned, line_of(host_vars, banned)),
                    )

        children = group.get("children")
        if isinstance(children, dict):
            for child in children.values():
                walk_group(child)

    # Top level of an inventory file is a mapping of group name -> group node.
    if isinstance(data, dict):
        for group in data.values():
            walk_group(group)
    return found


class NoHostOsVarsRule(AnsibleLintRule):
    """Host entries must not declare os_* metadata."""

    id = "no-host-os-vars"
    severity = "MEDIUM"
    tags = ["idiom"]
    version_changed = "1.0.0"
    _ids = {
        "no-host-os-vars[inventory]": "os_* metadata belongs in gathered facts",
    }

    def matchyaml(self, file: Lintable) -> list[MatchError]:
        if str(file.kind) != "inventory":
            return []
        from ruamel.yaml import YAML
        from ruamel.yaml.error import YAMLError

        try:
            data = YAML(typ="rt").load(file.content)
        except YAMLError:  # pragma: no cover - malformed YAML is load-failure's job
            # Only swallow parser errors; anything else is a real bug in this
            # rule or its dependencies and should surface rather than report a
            # false clean.
            return []

        # ansible-lint applies `# noqa` automatically to task- and play-level
        # matches, but not to a rule that walks an inventory tree itself, so
        # honor the comment explicitly against the offending line.
        lines = file.content.splitlines()

        def skipped(lineno: int) -> bool:
            if not 0 < lineno <= len(lines):
                return False
            skips = get_rule_skips_from_line(lines[lineno - 1], file, lineno)
            return bool({self.id, "no-host-os-vars[inventory]"}.intersection(skips))

        return [
            self.create_matcherror(
                f"Host '{host}' sets '{key}'. Per-host OS metadata duplicates "
                "gathered facts — read ansible_facts['os_family'] instead, or "
                "branch on inventory group membership when the host does not "
                "exist yet at render time.",
                filename=file,
                lineno=line,
                tag="no-host-os-vars[inventory]",
            )
            for host, key, line in find_host_os_vars(data)
            if not skipped(line)
        ]


# Loaded only under pytest — smoke test the tree walk.
if "pytest" in sys.modules:  # pragma: no cover
    import pytest
    from ruamel.yaml import YAML

    def _load(text: str) -> Any:
        return YAML(typ="rt").load(text)

    def test_flags_host_entry_key() -> None:
        data = _load(
            "all:\n"
            "  hosts:\n"
            "    web-1:\n"
            "      ansible_host: 10.0.0.1\n"
            "      os_family: RedHat\n",
        )
        assert find_host_os_vars(data) == [("web-1", "os_family", 5)]

    def test_flags_hosts_nested_under_children() -> None:
        data = _load(
            "all:\n"
            "  children:\n"
            "    rocky:\n"
            "      hosts:\n"
            "        db-1:\n"
            "          os_distribution: Rocky\n",
        )
        assert find_host_os_vars(data) == [("db-1", "os_distribution", 6)]

    def test_ignores_nested_structured_vars() -> None:
        # hyperv_guest_config.base_images.<name>.os_version is a different
        # namespace and must not be flagged.
        data = _load(
            "all:\n"
            "  hosts:\n"
            "    hv-1:\n"
            "      hyperv_guest_config:\n"
            "        base_images:\n"
            "          server2025:\n"
            "            os_version: '2025'\n",
        )
        assert find_host_os_vars(data) == []

    def test_ignores_group_vars_block() -> None:
        # Only `hosts:` entries are in scope, not a group's `vars:` block.
        data = _load(
            "all:\n"
            "  vars:\n"
            "    os_family: RedHat\n"
            "  hosts:\n"
            "    web-1:\n",
        )
        assert find_host_os_vars(data) == []

    def test_ignores_hosts_key_inside_a_free_form_var() -> None:
        # Regression: `vars:` is user data and may contain its own `hosts` key.
        # Traversal follows group structure only, so this must not be read as an
        # inventory host entry.
        data = _load(
            "all:\n"
            "  vars:\n"
            "    some_config:\n"
            "      hosts:\n"
            "        decoy:\n"
            "          os_version: '9'\n"
            "  hosts:\n"
            "    web-1:\n",
        )
        assert find_host_os_vars(data) == []

    def test_tolerates_bare_host_entries() -> None:
        data = _load("all:\n  hosts:\n    web-1:\n    web-2:\n")
        assert find_host_os_vars(data) == []

    @pytest.mark.parametrize("key", sorted(BANNED_KEYS))
    def test_every_banned_key_is_caught(key: str) -> None:
        data = _load(f"all:\n  hosts:\n    h:\n      {key}: x\n")
        assert [f for _, f, _ in find_host_os_vars(data)] == [key]

    # --- matchyaml(): file-kind scoping and noqa handling -------------------

    def _lint(tmp_path: Any, text: str, *, name: str = "hosts.yml") -> list[str]:
        """Run the rule over `text` written into an inventory/ dir."""
        from ansiblelint.file_utils import Lintable

        inv = tmp_path / "inventory"
        inv.mkdir(exist_ok=True)
        path = inv / name
        path.write_text(text)

        return [m.message for m in NoHostOsVarsRule().matchyaml(Lintable(str(path)))]

    def test_matchyaml_reports_host_entry(tmp_path: Any) -> None:
        messages = _lint(
            tmp_path,
            "all:\n  hosts:\n    web-1:\n      os_family: RedHat\n",
        )
        assert len(messages) == 1
        assert "web-1" in messages[0]
        assert "os_family" in messages[0]

    def test_matchyaml_honors_noqa(tmp_path: Any) -> None:
        assert (
            _lint(
                tmp_path,
                "all:\n"
                "  hosts:\n"
                "    switch-1:\n"
                "      os_distribution: EOS # noqa: no-host-os-vars\n",
            )
            == []
        )

    def test_matchyaml_noqa_is_per_line(tmp_path: Any) -> None:
        # The comment must only excuse the line it sits on.
        messages = _lint(
            tmp_path,
            "all:\n"
            "  hosts:\n"
            "    switch-1:\n"
            "      os_distribution: EOS # noqa: no-host-os-vars\n"
            "      os_version: '4.32'\n",
        )
        assert len(messages) == 1
        assert "os_version" in messages[0]

    def test_matchyaml_skips_non_inventory_files(tmp_path: Any) -> None:
        # A vars file is out of scope even with a host-shaped tree inside it.
        from ansiblelint.file_utils import Lintable

        path = tmp_path / "main.yml"
        path.write_text("all:\n  hosts:\n    web-1:\n      os_family: RedHat\n")
        lintable = Lintable(str(path))
        assert str(lintable.kind) != "inventory"
        assert NoHostOsVarsRule().matchyaml(lintable) == []
