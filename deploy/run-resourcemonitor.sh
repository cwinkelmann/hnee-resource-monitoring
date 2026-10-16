#!/bin/sh
# One launcher for every host: resolves this machine's python and listen address,
# then execs the monitor. Used by all three units and by resourcemonitor-guard.sh.
#
#   run-resourcemonitor.sh serve    # the dashboard and booking API
#   run-resourcemonitor.sh watch    # the collector (never posts; see the units)
#
# Why a wrapper at all: systemd will not take ExecStart's *executable* from a
# variable, so "put the python path in a config file" is impossible with
# EnvironmentFile alone. Resolving it here keeps one unit file per role across
# carrot (conda env, 10.188.1.1), olive (10.188.2.1) and the t14 (system python).
#
# Per-host values live in ~/.config/resourcemonitor/host.env (see host.env.example).
# That file is plain configuration, mode 0644. The Slack webhook is a credential and
# stays in its own 0600 file, read by systemd, never by this script.
set -eu

HOST_ENV="${RESOURCEMONITOR_HOST_ENV:-$HOME/.config/resourcemonitor/host.env}"

# A value already in the environment wins over the file, so a one-off override on
# the command line beats the host's defaults.
_bind="${RESOURCEMONITOR_BIND:-}"
_port="${RESOURCEMONITOR_PORT:-}"
_py="${RESOURCEMONITOR_PYTHON:-}"
_interval="${RESOURCEMONITOR_INTERVAL:-}"
if [ -f "$HOST_ENV" ]; then
    . "$HOST_ENV"
    [ -n "$_bind" ] && RESOURCEMONITOR_BIND="$_bind"
    [ -n "$_port" ] && RESOURCEMONITOR_PORT="$_port"
    [ -n "$_py" ] && RESOURCEMONITOR_PYTHON="$_py"
    [ -n "$_interval" ] && RESOURCEMONITOR_INTERVAL="$_interval"
fi

PY="${RESOURCEMONITOR_PYTHON:-}"
if [ -z "$PY" ]; then
    # The code is stdlib-only, so any 3.10+ interpreter runs it. Carrot and olive
    # use the conda env by the user's choice; a host without it falls back.
    if [ -x "$HOME/miniconda3/envs/resourcemonitor/bin/python" ]; then
        PY="$HOME/miniconda3/envs/resourcemonitor/bin/python"
    else
        PY="$(command -v python3)"
    fi
fi

# Fail closed: without a configured address the dashboard is loopback-only rather
# than silently exposed on a new machine's LAN.
BIND="${RESOURCEMONITOR_BIND:-127.0.0.1}"
PORT="${RESOURCEMONITOR_PORT:-8765}"
INTERVAL="${RESOURCEMONITOR_INTERVAL:-60}"

# Anything after the role is forwarded verbatim, which is how
# resourcemonitor.service passes --post. Dropping it would silence Slack
# without an error, so the forwarding is covered by a test.
case "${1:-}" in
    serve)
        shift
        exec "$PY" -m resourcemonitor serve --bind "$BIND" --port "$PORT" "$@"
        ;;
    watch)
        shift
        exec "$PY" -m resourcemonitor watch --interval "$INTERVAL" "$@"
        ;;
    *)
        echo "usage: $0 serve|watch (got: '${1:-}')" >&2
        exit 64
        ;;
esac
