#!/usr/bin/env bash
# USB input-current policy ported from zoompilot/jetlink 491cf1ef (MIT).
# Copyright (c) 2026-, Zeph Leggett. See third_party/jetlink/LICENSE.
# Keep a separately powered comma from drawing current from its USB host.
# Change the voter only with an empty port; never disconnect a USB-powered comma.
set -euo pipefail
umask 077
USBPD=${JETLINK_USBPD:-/sys/class/usbpd/usbpd0}
VOTER=${JETLINK_USB_ICL_VOTER:-/sys/kernel/debug/pmic-votable/USB_ICL}
STATE=${JETLINK_USB_POWER_STATE:-/dev/shm/carrot-jetlink-usb-power}
AGNOS=${JETLINK_AGNOS_MARKER:-/AGNOS}
fail() { echo "jetlink USB power: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail 'must run as root'
[[ -f "$AGNOS" ]] || fail 'requires AGNOS'
case "${1:-}" in off|on|status) ;; *) fail 'usage: usb_power.sh off|on|status';; esac
[[ -r "$USBPD/current_pr" && -r "$VOTER/force_active" && -r "$VOTER/force_val" ]] || fail 'USB policy engine or USB_ICL voter unavailable'
mkdir -p "$STATE"
[[ ! -L "$STATE" && $(stat -c %u "$STATE") == 0 ]] || fail 'state must be root-owned, without a symlink'
chmod 0700 "$STATE"
exec 9>"$STATE/lock"
flock -x 9
read -r active < "$VOTER/force_active"
read -r value < "$VOTER/force_val"
[[ "$active" =~ ^[01]$ && "$value" =~ ^-?[0-9]+$ ]] || fail 'unrecognized USB_ICL state'
if [[ "$1" == status ]]; then
  printf 'force_active=%s force_val=%s power_role=%s\n' "$active" "$value" "$(cat "$USBPD/current_pr")"
  exit 0
fi
# Already zero: no policy change, safe even if the port is now attached.
if [[ "$1" == off && "$active" == 1 && "$value" == 0 ]]; then echo disabled; exit 0; fi
if [[ "$1" == on && ! -f "$STATE/previous" ]]; then echo not-owned; exit 0; fi
[[ $(cat "$USBPD/current_pr") == none ]] || { echo deferred; exit 3; }
if [[ "$1" == off ]]; then
  [[ "$active" == 0 ]] || fail 'another forced USB_ICL limit is active; refusing to replace it'
  # Record only our immediately previous inactive voter; tmpfs expires at reboot.
  [[ -f "$STATE/previous" ]] || printf '%s\n' "$value" > "$STATE/previous"
  [[ $(cat "$USBPD/current_pr") == none ]] || { echo deferred; exit 3; }
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
  echo restored
fi
