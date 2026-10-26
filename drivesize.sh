#!/usr/bin/env bash
# Launch DriveSize. On WSL it runs under Windows Python by default, which scans
# C:, D: ... natively (much faster than WSL's /mnt). Use --linux to scan the
# WSL filesystem instead.
here="$(cd "$(dirname "$0")" && pwd)"
if [[ "$1" != "--linux" ]] && command -v python.exe >/dev/null 2>&1; then
  exec python.exe "$(wslpath -w "$here/drivesize.py")" "$@"
fi
[[ "$1" == "--linux" ]] && shift
exec python3 "$here/drivesize.py" "$@"
