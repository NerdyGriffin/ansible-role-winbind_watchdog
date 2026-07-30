# ansible-lint-rules

Custom [ansible-lint](https://ansible.readthedocs.io/projects/lint/) rules,
shared across repositories with `git subtree` so every consumer runs the same
copy.

## Rules

| Rule | Autofix | What it does |
| --- | --- | --- |
| `become-first` | yes | `become` and its `become_*` companions must sort directly after `name` in a task. |
| `no-host-os-vars` | no | A host entry in an inventory file must not declare `os_family`, `os_distribution`, `os_version` or `os_edition`. |

Both are opinionated conventions, not bug detectors. Adopt the ones you want;
`skip_list` or `warn_list` turns off the rest.

### `become-first`

```yaml
# Fails
- name: Install the package
  ansible.builtin.apt:
    name: htop
  become: true

# Passes
- name: Install the package
  become: true
  ansible.builtin.apt:
    name: htop
```

`ansible-lint --fix` hoists the `become` group automatically. Every other key
keeps its relative order, so the transform touches nothing else.

### `no-host-os-vars`

Per-host OS metadata duplicates gathered facts and drifts silently. Read
`ansible_facts['os_family']` instead, or branch on inventory group membership
when the host does not exist yet at render time.

```yaml
all:
  hosts:
    web-1:
      os_family: RedHat          # flagged
      hyperv_guest_config:
        base_images:
          server2025:
            os_version: '2025'   # not flagged — different namespace
```

Scope is deliberately narrow: only keys set directly on a host entry under a
`hosts:` mapping in an inventory file. For a device that genuinely cannot gather
facts (a switch, a UPS card), record the OS in a comment, or use
`# noqa: no-host-os-vars` if it must be a var.

## Use in a repository

Vendor this repo into `.ansible-lint-rules/` with `git subtree`:

```bash
git remote add lint-rules https://github.com/NerdyGriffin/ansible-lint-rules.git
git subtree add --prefix .ansible-lint-rules lint-rules main --squash
```

Then point `.ansible-lint` at the directory:

```yaml
rulesdir:
  - .ansible-lint-rules
```

The rules are committed into the consumer, so CI needs no extra step — it lints
with the vendored copy.

To get later changes:

```bash
git subtree pull --prefix .ansible-lint-rules lint-rules main --squash
```

To send a local fix back:

```bash
git subtree push --prefix .ansible-lint-rules lint-rules <branch>
```

## Tests

Each rule carries its smoke tests inside the module, behind
`if "pytest" in sys.modules:` — the upstream ansible-lint idiom. `pytest.ini`
collects them:

```bash
pip install ansible-lint pytest
pytest
```

## License

MIT
