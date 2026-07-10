# nerdygriffin.winbind_watchdog

An Ansible role that deploys a systemd timer to probe **winbind** health on AD
member hosts and, when it detects a wedged or degraded state, runs a targeted
recovery: `pkill -9 winbindd` → `kinit -k '<HOST>$@<REALM>'` →
`systemctl restart winbind`. Intended for realm-joined AD member servers (e.g.
Samba file servers, `idmap_ad` clients) where winbindd can silently wedge on
trust or `idmap_ad` operations — typically after a DC blip or machine-password
rotation.

> **History:** this role supersedes the standalone
> [`winbind-watchdog`](https://github.com/NerdyGriffin/winbind-watchdog) RPM
> package. The RPM required an EL host and a separate build/install channel;
> packaging the same probe/recovery script as a role makes it distro-agnostic
> (it's pure `bash` + systemd) and lets Ansible manage state declaratively.

## Why not just `systemctl restart winbind`?

When winbindd is wedged, a plain restart often doesn't clear it — the machine
account's Kerberos ticket needs refreshing from the keytab and any hung workers
must be force-killed first. The role installs exactly that sequence, run only
when a probe says it's needed.

## Detection

### Mode 1 — wedged trust state (always on)

`timeout $PROBE_TIMEOUT wbinfo -t` fails with `WBC_ERR_WINBIND_NOT_AVAILABLE`
while `wbinfo -p`/`wbinfo -P` still succeed — winbindd is alive and the DC is
reachable, but the trust state machine is wedged.

### Mode 2 — idmap_ad broken while `wbinfo -t` stays green (opt-in)

Set `winbind_watchdog_idmap_probe_user` to a known-resolvable AD account
(`DOMAIN\user`) to also run `getent passwd` against it. This catches two
sub-modes the trust probe misses:

- **2a — hang:** the lookup blocks until timeout (e.g. expired machine-account
  TGT; winbind passes the RPC trust check but can't bind LDAP for SID→UID).
- **2b — fast-fail:** a known-good account resolves empty/error *immediately*
  (e.g. `WBC_ERR_DOMAIN_NOT_FOUND`). Observed in the wild with `wbinfo -t` green
  and DC ports open, yet `id`/`wbinfo -i/-u/-n` and `getent` all returning empty
  fast, `smbd` denying group-gated shares. A plain `systemctl restart winbind`
  clears it.

**Fast-fail is ambiguous** — the same empty result appears whether winbind is
degraded (recoverable) or `winbind_watchdog_idmap_probe_user` is misconfigured
(a restart will never help). The role handles this without a restart loop: the
first fast-fail triggers recovery; if the post-recovery re-probe *still*
fast-fails, it records a cooldown (`winbind_watchdog_idmap_fastfail_cooldown`
seconds, default 1h) and backs off — at most one recovery attempt per window
until the operator fixes the probe account.

## Role variables

| Variable | Default | Purpose |
|---|---|---|
| `winbind_watchdog_manage` | `true` | Master on/off for the role. |
| `winbind_watchdog_probe_timeout` | `10` | Per-probe timeout (seconds). |
| `winbind_watchdog_recovery_grace` | `3` | Wait after restart before re-probe. |
| `winbind_watchdog_idmap_probe_user` | `""` | `DOMAIN\user` to enable the mode-2 probe. Empty = trust-only. |
| `winbind_watchdog_idmap_fastfail_cooldown` | `3600` | Fast-fail back-off (seconds). `0` = timeout-only mode-2. |
| `winbind_watchdog_machine_principal` | `""` | Override the `kinit -k` principal (else auto-derived). |
| `winbind_watchdog_realm` | `""` | Realm, if `default_realm` is unset in `/etc/krb5.conf`. |
| `winbind_watchdog_dry_run` | `false` | Log what recovery would do without acting. |

## Example

```yaml
- name: Deploy winbind-watchdog to winbind members
  hosts: winbind_members
  become: true
  roles:
    - role: nerdygriffin.winbind_watchdog
      winbind_watchdog_idmap_probe_user: 'EXAMPLE\svc-probe'
```

## Requirements

- A realm-joined AD member host with `samba-winbind` and `krb5` tooling
  (`wbinfo`, `getent`, `kinit`) installed and a valid machine keytab.
- `/etc/krb5.conf` with a working `default_realm` (or set
  `winbind_watchdog_machine_principal`/`winbind_watchdog_realm`).

## Operations

- Event log: `/var/log/winbind-watchdog.log` (rotated).
- Timer state: `systemctl list-timers winbind-watchdog.timer`.
- Manual one-shot (same as the timer): `sudo /usr/local/sbin/winbind-watchdog.sh`.

## License

MIT
