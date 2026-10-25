# HomeShare

Share files between the computers and phones in your home, from any web browser. No accounts, no internet uploads, no setup.

## Download

Get **HomeShare.exe** from the [latest release](../../releases/latest), then double-click it.

- Windows may say "Windows protected your PC" the first time: click **More info → Run anyway**.
- Windows asks once to allow HomeShare to make changes: click **Yes** (this lets your other devices connect).

## How it works

1. Open HomeShare on the computer that has the files. A page opens in your browser with a link and a QR code.
2. On a phone or another computer on the same Wi-Fi, open the link or scan the code.
3. Everyone sees the same files: view photos and videos, upload, download, make folders, and leave notes.

Your files never leave your home network.

## Features

- Photo, video, music, PDF and text previews, with thumbnails (including album art and document pages)
- Upload whole folders; download several files as one zip
- Shared notes with formatting, links and attachments
- Optional PIN, secret link, or approval for each new device
- Pauses automatically on Wi-Fi networks that aren't your home network
- Tray icon, automatic stop timer, and an activity log on the sharing computer

## For developers

`homeshare.py` is the whole app (a Python web server plus its page). Run it with `python homeshare.py`.

- Tests: `python test_homeshare.py`
- Build the Windows program: raise `VERSION` in `homeshare.py`, then `python build.py`
