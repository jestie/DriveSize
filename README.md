# DriveSize

A TreeSize-style disk usage explorer: drive overview, donut charts, folder
drill-down, file types, largest files, cleanup suggestions, duplicate finder,
and delete (Recycle Bin by default).

Python standard library only; no installs.

## Run

| Where | How |
|---|---|
| Windows (fast, scans C:, D: ... natively) | double-click `DriveSize.bat`, or `./drivesize.sh` from WSL |
| WSL / Linux filesystem | `./drivesize.sh --linux` |

It opens `http://127.0.0.1:8765/` in your browser. Ctrl+C in the terminal stops it.

## Safety

- Binds to 127.0.0.1 only; every API call needs a per-run token embedded in the page.
- Deletes go to the Recycle Bin (Windows) or Trash (Linux) unless "permanently" is ticked.
- Refuses drive roots, `Windows`, `Program Files`, `ProgramData`, `C:\Users`, user
  profile roots, page/hibernate files, WSL `ext4.vhdx`, and Linux system dirs.
- Cleanup verdicts are heuristics: "Safe" = temp/cache folders apps rebuild;
  "Review" = probably unneeded (installers, logs, venvs, node_modules, old media).
  Nothing inside system areas is ever suggested.
