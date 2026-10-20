#!/usr/bin/env bash
# USB input-current policy ported from zoompilot/jetlink 491cf1ef (MIT).
# Copyright (c) 2026-, Zeph Leggett. See third_party/jetlink/LICENSE.
# Keep a separately powered comma from drawing current from its USB host.
# Default to an empty port. An explicit independent-power installation can also
# block input while attached; it must always retain its separate main supply.
set -euo pipefail
umask 077
USBPD=${JETLINK_USBPD:-/sys/class/usbpd/usbpd0}
VOTER=${JETLINK_USB_ICL_VOTER:-/sys/kernel/debug/pmic-votable/USB_ICL}
STATE=${JETLINK_USB_POWER_STATE:-/dev/shm/carrot-jetlink-usb-power}
AGNOS=${JETLINK_AGNOS_MARKER:-/AGNOS}
POLICY=${JETLINK_USB_POWER_POLICY_DIR:-/data/carrot-jetlink-power}
CONFIRMED="$POLICY/independent-power"
fail() { echo "jetlink USB power: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail 'must run as root'
[[ -f "$AGNOS" ]] || fail 'requires AGNOS'
case "${1:-}" in off|on|status|independent) ;; *) fail 'usage: usb_power.sh off|on|status|independent';; esac
[[ -r "$USBPD/current_pr" && -r "$VOTER/force_active" && -r "$VOTER/force_val" ]] || fail 'USB policy engine or USB_ICL voter unavailable'
mkdir -p "$STATE"
[[ ! -L "$STATE" && $(stat -c %u "$STATE") == 0 ]] || fail 'state must be root-owned, without a symlink'
chmod 0700 "$STATE"
exec 9>"$STATE/lock"
flock -x 9
read -r active < "$VOTER/force_active"
read -r value < "$VOTER/force_val"
[[ "$active" =~ ^[01]$ && "$value" =~ ^-?[0-9]+$ ]] || fail 'unrecognized USB_ICL state'
independent_power() {
  [[ -e "$CONFIRMED" || -L "$CONFIRMED" ]] || return 1
  [[ -d "$POLICY" && ! -L "$POLICY" && $(stat -c %u "$POLICY") == 0 ]] || fail 'untrusted independent-power directory'
  [[ -f "$CONFIRMED" && ! -L "$CONFIRMED" && $(stat -c %u "$CONFIRMED") == 0 ]] || fail 'untrusted independent-power confirmation'
  local mode
  for entry in "$POLICY" "$CONFIRMED"; do
    mode=$(stat -c %a "$entry")
    [[ $((8#$mode & 0022)) == 0 ]] || fail 'independent-power policy is writable by others'
  done
  [[ $(cat "$CONFIRMED") == confirmed ]] || fail 'unrecognized independent-power confirmation'
}
if [[ "$1" == status ]]; then
  policy=empty-port-only
  if independent_power; then policy=independent; fi
  printf 'force_active=%s force_val=%s power_role=%s policy=%s\n' "$active" "$value" "$(cat "$USBPD/current_pr")" "$policy"
  exit 0
fi
action=$1
if [[ "$action" == independent ]]; then
  # Explicit owner enrollment, not USB-host/voltage-based supply discovery.
  [[ "$active" == 0 || ( "$active" == 1 && "$value" == 0 ) ]] || fail 'another forced USB_ICL limit is active; refusing to replace it'
  if [[ ! -e "$POLICY" && ! -L "$POLICY" ]]; then mkdir -m 0755 "$POLICY"; fi
  [[ -d "$POLICY" && ! -L "$POLICY" && $(stat -c %u "$POLICY") == 0 ]] || fail 'untrusted independent-power directory'
  mode=$(stat -c %a "$POLICY")
  [[ $((8#$mode & 0022)) == 0 ]] || fail 'independent-power directory is writable by others'
  chmod 0755 "$POLICY"
  if [[ -e "$CONFIRMED" || -L "$CONFIRMED" ]]; then independent_power || fail 'invalid independent-power policy'; fi
  temporary=$(mktemp "$POLICY/.independent.XXXXXX")
  trap 'rm -f -- "$temporary"' EXIT
  printf 'confirmed\n' > "$temporary"
  chmod 0644 "$temporary"
  mv -T -- "$temporary" "$CONFIRMED"
  trap - EXIT
  action=off
fi
# Already zero: no policy change, safe even if the port is now attached.
if [[ "$action" == off && "$active" == 1 && "$value" == 0 ]]; then echo disabled; exit 0; fi
may_disable() { [[ $(cat "$USBPD/current_pr") == none ]] || independent_power; }
if [[ "$action" == off ]]; then may_disable || { echo deferred; exit 3; }
else [[ $(cat "$USBPD/current_pr") == none ]] || { echo deferred; exit 3; }; fi
if [[ "$action" == on && ! -f "$STATE/previous" ]]; then
  if independent_power; then rm -- "$CONFIRMED"; fi
  echo not-owned; exit 0
fi
if [[ "$action" == off ]]; then
  [[ "$active" == 0 ]] || fail 'another forced USB_ICL limit is active; refusing to replace it'
  # Record only our immediately previous inactive voter; tmpfs expires at reboot.
  [[ -f "$STATE/previous" ]] || printf '%s\n' "$value" > "$STATE/previous"
  may_disable || { echo deferred; exit 3; }
  echo 0 > "$VOTER/force_val"
  echo 1 > "$VOTER/force_active"
  [[ $(cat "$VOTER/force_active") == 1 && $(cat "$VOTER/force_val") == 0 ]] || fail 'USB_ICL zero-current verification failed'
  echo disabled
else
  # Explicit restore only, never daemon cleanup or a transport-mode switch.
  read -r previous < "$STATE/previous"
  [[ "$previous" =~ ^-?[0-9]+$ ]] || fail 'unrecognized previous USB_ICL value'
  [[ "$active" == 1 && "$value" == 0 ]] || fail 'USB_ICL changed since our apply; refusing to restore'
  [[ $(cat "$USBPD/current_pr") == none ]] || { echo deferred; exit 3; }
  echo 0 > "$VOTER/force_active"
  echo "$previous" > "$VOTER/force_val"
  rm -- "$STATE/previous"
  if independent_power; then rm -- "$CONFIRMED"; fi
  echo restored
fi
