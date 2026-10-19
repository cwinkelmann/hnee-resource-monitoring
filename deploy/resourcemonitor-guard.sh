#!/bin/sh
# Fallback supervisor for hosts where systemd user units cannot be used.
#
# The units (resourcemonitor-dry.service, resourcemonitor-web.service) are the
# preferred way to run this and the only properly supervised one. They need
# Linger=yes for the user, which needs root:
#
#   sudo loginctl enable-linger "$(id -un)"
#
# Carrot already has it. On olive there is no sudo and never will be, so olive
# runs this script from cron permanently -- it is that host's supported path,
# not a stopgap.
#
# Without linger, systemd stops the whole user manager when the last session
# closes, so a unit would die with the SSH session that enabled it. Plain
# processes survive (KillUserProcesses=no), so on such a host this script plays
# Restart=always instead, from cron:
#
#   @reboot      sleep 30 && $HOME/ResourceMonitor/deploy/resourcemonitor-guard.sh
#   */5 * * * *  $HOME/ResourceMonitor/deploy/resourcemonitor-guard.sh
#
# It starts whichever of the two roles is missing and leaves a running one alone,
# so it is safe to run every five minutes. Bind address, port and interpreter come
# from ~/.config/resourcemonitor/host.env, exactly as the units get them: this
# script only decides *whether* to start, run-resourcemonitor.sh decides *how*.
set -eu

ROOT="$HOME/ResourceMonitor"
RUN="$ROOT/deploy/run-resourcemonitor.sh"
LOGDIR="$HOME/.local/state/resourcemonitor"

mkdir -p "$LOGDIR"
cd "$ROOT"

# Match on the module invocation, not the word "resourcemonitor", which would also
# match this script's own path and the conda env inside the python binary.
start_if_missing() {
    role="$1"
    if ! pgrep -f -- "resourcemonitor $role" >/dev/null 2>&1; then
        setsid "$RUN" "$role" >>"$LOGDIR/$role.log" 2>&1 &
        echo "$(date -Is) started: $role" >>"$LOGDIR/guard.log"
    fi
}

start_if_missing watch
start_if_missing serve
