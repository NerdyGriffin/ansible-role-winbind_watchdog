#!/bin/bash
# winbind-watchdog.sh — detect hung/degraded winbind idmap/trust state and recover.
#
# Deployed by the Ansible role nerdygriffin.winbind_watchdog. Configuration is
# rendered to /etc/winbind-watchdog.conf from the role's variables; edit those,
# not this script.
#
# Symptoms this handles:
#   - Mode 1 — wedged trust state: `wbinfo -t` returns WBC_ERR_WINBIND_NOT_AVAILABLE
#     while `wbinfo -p` and `wbinfo -P` still report healthy (winbindd is alive
#     but wedged on idmap_ad / trust credential operations, often after DC
#     maintenance or machine-password rotation). smbd logs
#     "check_account: Failed to convert SID ... to a UID".
#   - Mode 2a — idmap_ad LDAP path HANGS while RPC stays green: `wbinfo -t`
#     succeeds but `getent passwd 'DOMAIN\user'` and `wbinfo -i` block until
#     timeout. Trigger seen in the wild: cached machine-account TGT expired
#     without auto-renewal; winbind kept passing the RPC trust check but
#     couldn't bind LDAP for SID->UID mapping.
#   - Mode 2b — idmap_ad path FAST-FAILS while RPC stays green: `wbinfo -t`
#     succeeds but a KNOWN-good account resolves empty/error *immediately*
#     (e.g. WBC_ERR_DOMAIN_NOT_FOUND). Real incident 2026-07-10 on hl-fs44:
#     `wbinfo -t`/`wbinfo -P` green and all DC ports open, yet `id`, `wbinfo -i`,
#     `wbinfo -u`, `wbinfo -n`, and `getent passwd/group` all returned empty
#     fast; smbd spammed "token_contains_name: lookup_name '...' failed
#     NT_STATUS_NO_SUCH_DOMAIN" and new SMB logins gated on group membership
#     were denied. A plain `systemctl restart winbind` cleared it. A pure
#     timeout probe (mode 2a only) does NOT catch this — see probe_idmap().
#
# Recovery sequence:
#   1. pkill -9 winbindd           — force-kill hung workers
#   2. kinit -k $MACHINE_PRINCIPAL — refresh Kerberos ticket from keytab
#   3. systemctl restart winbind   — clean start
#
# Mode 1 is detected by `wbinfo -t`. Mode 2 (a and b) requires a second probe
# against a known-resolvable AD account — see IDMAP_PROBE_USER.

set -u

LOG="/var/log/winbind-watchdog.log"
CONF="/etc/winbind-watchdog.conf"

# Fast-fail cooldown state. /run is tmpfs, so this self-clears on reboot
# (after which winbind starts fresh and the ambiguity resets).
COOLDOWN_STATE="/run/winbind-watchdog.idmap-fastfail"

# Defaults (override via $CONF)
PROBE_TIMEOUT=10
PROBE_CMD=(wbinfo -t)
RECOVERY_GRACE=3
MACHINE_PRINCIPAL=""   # auto-detected if empty
REALM=""               # auto-detected from /etc/krb5.conf if empty
DRY_RUN=0
# Optional second probe — exercises full nsswitch (idmap_ad -> LDAP). Set to
# a known-resolvable AD account (e.g. 'NERDYGRIFFIN\christian.admin') to
# catch mode-2 failures where wbinfo -t passes but SID<->UID/name lookups
# fail. Empty (default) = disabled, behaves like the trust-only watchdog.
IDMAP_PROBE_USER=""
# Fast-fail cooldown (seconds). When the idmap probe FAST-FAILS (returns
# empty/error immediately, not a timeout) for a known-good account, the first
# occurrence triggers recovery. If recovery does NOT clear it, we assume the
# probe account is misconfigured (typo / removed) rather than winbind being
# wedged, and back off for this many seconds before trying again — so a bad
# IDMAP_PROBE_USER causes at most one restart per window, not a restart loop
# every timer tick. Set to 0 to disable fast-fail recovery entirely (revert
# to timeout-only mode-2 detection).
IDMAP_FASTFAIL_COOLDOWN=3600

# shellcheck disable=SC1090
[[ -f "$CONF" ]] && . "$CONF"

log() { printf '%s: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG"; }

auto_detect_principal() {
    [[ -n "$MACHINE_PRINCIPAL" ]] && return 0
    local realm="$REALM"
    if [[ -z "$realm" && -r /etc/krb5.conf ]]; then
        realm=$(awk '/^[[:space:]]*default_realm[[:space:]]*=/ { print $3; exit }' /etc/krb5.conf)
    fi
    if [[ -z "$realm" ]]; then
        log "ERROR: cannot determine Kerberos realm (set REALM or MACHINE_PRINCIPAL in $CONF)"
        return 1
    fi
    local host_upper
    host_upper=$(hostname -s | tr '[:lower:]' '[:upper:]')
    MACHINE_PRINCIPAL="${host_upper}\$@${realm}"
    return 0
}

# Remaining seconds on the fast-fail cooldown; echoes 0 when inactive/expired.
fastfail_cooldown_remaining() {
    [[ -f "$COOLDOWN_STATE" ]] || { echo 0; return; }
    local now mtime age
    now=$(date +%s)
    mtime=$(stat -c %Y "$COOLDOWN_STATE" 2>/dev/null) || { echo 0; return; }
    age=$(( now - mtime ))
    if (( age < IDMAP_FASTFAIL_COOLDOWN )); then
        echo $(( IDMAP_FASTFAIL_COOLDOWN - age ))
    else
        echo 0
    fi
}
mark_fastfail_cooldown() { : > "$COOLDOWN_STATE" 2>/dev/null || true; }
clear_fastfail_cooldown() { rm -f "$COOLDOWN_STATE" 2>/dev/null || true; }

probe() {
    timeout "$PROBE_TIMEOUT" "${PROBE_CMD[@]}" >/dev/null 2>&1
}

# Second probe — only runs if IDMAP_PROBE_USER is configured. Returns:
#   0  healthy (or not configured, or fast-fail currently suppressed by cooldown)
#   1  wedged via TIMEOUT (mode 2a)
#   2  wedged via FAST-FAIL (mode 2b) — recoverable, cooldown-guarded
#
# The fast-fail case is ambiguous: a known-good account resolving empty/error
# is EITHER winbind degradation that a restart fixes (mode 2b) OR a genuinely
# bad IDMAP_PROBE_USER (typo / removed account) that a restart will NEVER fix.
# We can't tell from one probe, so: treat the first occurrence as a wedge and
# recover; if the post-recovery re-probe STILL fast-fails, main() records a
# cooldown and we back off (assume config issue) until it expires.
probe_idmap() {
    [[ -z "$IDMAP_PROBE_USER" ]] && return 0
    local out rc
    out=$(timeout "$PROBE_TIMEOUT" getent passwd "$IDMAP_PROBE_USER" 2>>"$LOG")
    rc=$?
    if (( rc == 124 )); then
        log "idmap probe TIMEOUT: 'getent passwd $IDMAP_PROBE_USER' did not return within ${PROBE_TIMEOUT}s"
        return 1
    fi
    if (( rc != 0 )) || [[ -z "$out" ]]; then
        local empty=no
        [[ -z "$out" ]] && empty=yes
        if (( IDMAP_FASTFAIL_COOLDOWN <= 0 )); then
            log "idmap probe fast-fail (rc=$rc, empty=$empty); IDMAP_FASTFAIL_COOLDOWN<=0 so NOT treating fast-fail as a wedge — check IDMAP_PROBE_USER='$IDMAP_PROBE_USER'"
            return 0
        fi
        local remain
        remain=$(fastfail_cooldown_remaining)
        if (( remain > 0 )); then
            log "idmap probe fast-fail (rc=$rc) but a prior recovery did not clear it — in cooldown (${remain}s left); treating as config issue (check IDMAP_PROBE_USER='$IDMAP_PROBE_USER'), not recovering"
            return 0
        fi
        log "idmap probe FAST-FAIL: 'getent passwd $IDMAP_PROBE_USER' returned rc=$rc/empty for a known account — treating as winbind degradation, will recover"
        return 2
    fi
    # Healthy resolution — clear any stale cooldown so the next fast-fail is
    # treated as a fresh event.
    clear_fastfail_cooldown
    return 0
}

recover() {
    log "recovery: starting (principal=$MACHINE_PRINCIPAL)"
    if (( DRY_RUN )); then
        log "DRY_RUN: would run pkill -9 winbindd; kinit -k $MACHINE_PRINCIPAL; systemctl restart winbind"
        return 0
    fi

    pkill -9 winbindd 2>/dev/null || true
    sleep 2

    if kinit -k "$MACHINE_PRINCIPAL" >>"$LOG" 2>&1; then
        log "recovery: kinit ok"
    else
        log "recovery: kinit FAILED (will still attempt winbind restart)"
    fi

    if systemctl restart winbind >>"$LOG" 2>&1; then
        log "recovery: winbind restarted"
    else
        log "recovery: systemctl restart winbind FAILED"
        return 1
    fi

    sleep "$RECOVERY_GRACE"
    return 0
}

main() {
    auto_detect_principal || exit 2

    local primary_ok idmap_rc
    if probe; then primary_ok=1; else primary_ok=0; fi
    probe_idmap; idmap_rc=$?

    if (( primary_ok && idmap_rc == 0 )); then
        exit 0
    fi

    (( ! primary_ok )) && log "primary probe failed: '${PROBE_CMD[*]}' did not return success within ${PROBE_TIMEOUT}s"
    (( idmap_rc != 0 )) && log "idmap probe failed (rc=$idmap_rc; see prior log line for details)"

    recover || { log "recovery: aborted"; exit 1; }

    local primary_ok2 idmap_rc2
    if probe; then primary_ok2=1; else primary_ok2=0; fi
    probe_idmap; idmap_rc2=$?

    if (( primary_ok2 && idmap_rc2 == 0 )); then
        log "recovery: succeeded — winbind is healthy again"
        clear_fastfail_cooldown
        exit 0
    fi

    if (( idmap_rc2 == 2 )); then
        # Fast-fail survived a full recovery -> almost certainly a bad
        # IDMAP_PROBE_USER, not a wedge. Start the cooldown so we don't
        # restart winbind every timer tick chasing a config error.
        mark_fastfail_cooldown
        log "recovery: idmap still FAST-FAILING after restart — likely IDMAP_PROBE_USER config error; backing off for ${IDMAP_FASTFAIL_COOLDOWN}s"
    fi

    log "recovery: FAILED — probes still failing after restart (primary=$primary_ok2 idmap_rc=$idmap_rc2); manual intervention may be required"
    exit 1
}

main "$@"
