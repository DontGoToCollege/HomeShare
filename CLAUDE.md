# HomeShare

Share a folder with phones and computers on a home network: one Python file (`homeshare.py`) packaged as a windowless Windows exe.

## Who it's for
People with no computer experience. Anything technical must be handled for them or hidden.
- No console, cmd or Python windows, ever. Errors show as a message box.
- Every setting is changed in the browser page on the sharing computer (the Settings tab), never through scripts, flags or config files.
- Works out of the box: HomeShare sets up its own Windows Firewall rule (one "Yes" click) instead of explaining firewalls. Sharing is open on the home network by default; the user can tighten it in Settings (link code, PIN, approval).

## Files
- `homeshare.py`: the whole app: server and page (the page is the `PAGE` string near the end).
- `test_homeshare.py`: run `python test_homeshare.py` before every build. It must pass with and without the optional libraries.
- `build.py`: tests, builds `HomeShare.exe`, and files a copy in `Versions\Archive\HomeShare-<VERSION>.exe`. Raise `VERSION` at the top of `homeshare.py` first. It refuses a version that's already archived.
- Optional libraries bundled in the exe: Pillow, pystray, segno, mutagen, pypdfium2. The script still runs without them.

## Lessons learned (don't repeat)
- Uploads: one request per file (up to 3 files at once). Splitting files into pieces broke iPhone Safari uploads.
- Upload speed between devices is limited by the home Wi-Fi path, not HomeShare. Check with the Files tab's "Test connection speed" before changing upload code.
- Open the browser with `open_browser()` (via Explorer), never directly, so closing HomeShare never closes the user's browser.
- One HomeShare tab per browser: `localStorage["hs-current"]` names the current tab; opening, typing the link or refreshing claims it, automatic reloads don't. Retired tabs must stop all polling (or they revive after a restart). In-page navigation uses `location.replace` so tabs stay closable.
- Pages hold `/api/live` open so they learn within seconds that HomeShare stopped (the firewall makes plain checks from phones time out).
- If HomeShare is running when a new build needs to replace HomeShare.exe, close it yourself without asking, then start the new version again (`build.py` does both: graceful stop, force if needed, reopen in the background via Windows so it outlives the build). The owner asked for this for HomeShare only, not as a rule for other projects.
- Smoke-test a new exe with a throwaway `USERPROFILE` whose `.homeshare.json` has `"lan": false`, so it never touches the user's real settings or firewall.
