#!/usr/bin/env python3
"""HomeShare: share a folder with everyone on your home network.

Double-click to run (or: python homeshare.py [folder]). Files live in ~/HomeShare by default.
A browser opens on the Home tab with the share link and QR code; other devices on the same Wi-Fi open that link.
All settings are on that page, visible only on the sharing computer.
Optional extras (bundled in the .exe): Pillow (thumbnails, tray icon), pystray (tray icon), segno (QR code).
"""
import base64, ctypes, hashlib, hmac, html, io, ipaddress, itertools, json, mimetypes, os, re, secrets, shutil, socket, struct, subprocess
import sys, threading, time, urllib.request, webbrowser, zipfile
from collections import deque
from html.parser import HTMLParser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:
    Image = None  # thumbnails fall back to the original image; no tray icon
try:
    import segno
except ImportError:
    segno = None
try:
    import mutagen  # album art inside music files
    from mutagen.flac import Picture
    from mutagen.id3 import ID3
except ImportError:
    mutagen = None
try:
    import pypdfium2 as pdfium  # first page of PDFs
except Exception:
    pdfium = None
try:
    import pystray
except Exception:  # ImportError, or no desktop to show a tray on
    pystray = None

VERSION = "1.7.0"  # raise for every release: build.py files each version under Versions/Archive
CONFIG = Path.home() / ".homeshare.json"
TRASH, THUMBS, NOTES_FILE = ".trash", ".thumbs", ".homeshare-notes.json"
PORTS = (80, 8080, 8000, 8888)  # 80 gives the shortest link (no ":8080" on the end)
THUMB = 360
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus"}
OFFICE_EXT = {".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm", ".odt", ".ods", ".odp", ".odg"}
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log", ".xml", ".ini", ".cfg", ".yml", ".yaml", ".css", ".js", ".py", ".html", ".htm", ".srt"}
MAX_DRAIN = 64 << 20  # an upload we turn down: read and discard at most this much, else just hang up
RANGE_PIECE = 4 << 20  # most a video gets per request, so a paused video can't hog one of the phone's few connections
RESERVE = 5 << 30  # keep this much disk free: uploads stop before the computer runs out of space
STOP_DEFAULT = 4  # hours; sharing is meant to be short-term
LOCK, LOGIN_LOCK, NOTES_LOCK = threading.Lock(), threading.Lock(), threading.Lock()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SCRIPTABLE = {"text/html", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/xml"}
mimetypes.add_type("video/mp4", ".mov")  # shortcut: most phone .mov files are H.264 that browsers play as mp4; HEVC ones still won't
PAGE_CSP = ("default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' https: http:; "
            "media-src 'self'; frame-src 'self' https://www.youtube-nocookie.com; connect-src 'self'; form-action 'self'; "
            "base-uri 'none'; object-src 'none'; frame-ancestors 'none'")

OPEN_PATHS = {"/", "/api/info", "/api/login", "/api/live"}  # reachable before getting past the link / PIN / approval checks
EDIT_PATHS = {"/api/mkdir", "/api/thumbreset", "/api/move", "/api/delete", "/api/notes", "/api/notes/delete"}
HOST_PATHS = {"/api/link", "/api/settings", "/api/firewall", "/api/open", "/api/folder", "/api/stop", "/api/restore",
              "/api/emptytrash", "/api/devices", "/api/device", "/api/activity", "/api/trust", "/api/newkey",
              "/api/activity/clear", "/api/activity/remove", "/api/activity/reclaim"}
NEED_MSG = {"link": "Open the full link from the person sharing.", "pin": "Enter the PIN first.",
            "approval": "Waiting for the sharing computer to let this device in.",
            "blocked": "The sharing computer removed this device.", "paused": "Sharing is paused right now."}
RESERVED_CODES = {"api", "f", "thumb", "up", "zip", "k"}  # first parts of HomeShare's own addresses


def clean_code(s):
    """The optional code at the end of the link, e.g. "photos" in http://family-files.local/photos."""
    return re.sub(r"[^a-z0-9-]", "", s.strip().lower().replace(" ", "-"))[:40]


def code_from_path(path):
    code = (path[3:] if path.startswith("/k/") else path[1:]).lower()  # /k/<code> is how older links looked
    return code if re.fullmatch(r"[a-z0-9-]{1,40}", code) and code not in RESERVED_CODES else None


class Refused(Exception):
    """A request we turn down with a message the user should see."""


def load_cfg():
    try:
        return json.loads(CONFIG.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_cfg(**kv):
    CONFIG.write_text(json.dumps({**load_cfg(), **kv}), "utf-8")


def alert(msg):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, msg, "HomeShare", 0x40)
    else:
        print(msg)


def copy_n(src, dst, n):
    """Copy n bytes (dst None = discard). Returns how many bytes were missing if src ran out."""
    while n > 0:
        b = src.read(min(n, 1 << 20))
        if not b:
            break
        if dst:
            dst.write(b)
        n -= len(b)
    return n


def audio_cover(p):
    """Album art stored inside an MP3, M4A, FLAC or Ogg file, or None."""
    if p.suffix.lower() == ".mp3":
        pics = ID3(p).getall("APIC")
        return pics[0].data if pics else None
    f = mutagen.File(p)
    if f is None:
        return None
    if getattr(f, "pictures", None):  # FLAC
        return f.pictures[0].data
    tags = f.tags or {}
    if "covr" in tags:  # M4A
        return bytes(tags["covr"][0])
    if "metadata_block_picture" in tags:  # Ogg / Opus
        return Picture(base64.b64decode(tags["metadata_block_picture"][0])).data
    return None


def preview_image(p):
    """A picture to make the thumbnail from: the photo itself, album art, a document's first page or saved preview."""
    ext = p.suffix.lower()
    if ext in AUDIO_EXT:
        data = audio_cover(p) if mutagen else None
        return Image.open(io.BytesIO(data)) if data else None
    if ext == ".pdf":
        if not pdfium:
            return None
        pdf = pdfium.PdfDocument(str(p))
        try:
            page = pdf[0]
            return page.render(scale=THUMB / max(page.get_size())).to_pil().copy()
        finally:
            pdf.close()
    if ext in OFFICE_EXT:  # Word/Excel/PowerPoint/OpenOffice files often carry a preview picture inside
        with zipfile.ZipFile(p) as z:
            for name in ("docProps/thumbnail.jpeg", "docProps/thumbnail.png", "Thumbnails/thumbnail.png"):
                if name in z.namelist():
                    return Image.open(io.BytesIO(z.read(name)))
        return None
    if ext in TEXT_EXT:  # a snapshot of the first lines
        with p.open("rb") as f:
            lines = f.read(4000).decode("utf-8", "replace").splitlines()[:20]
        im = Image.new("RGB", (THUMB * 5 // 6, THUMB), "white")
        draw = ImageDraw.Draw(im)
        try:
            font = ImageFont.load_default(size=14)
        except TypeError:  # older Pillow: small built-in font
            font = ImageFont.load_default()
        for i, line in enumerate(lines):
            draw.text((14, 12 + i * 17), line.expandtabs(2)[:40], fill=(60, 60, 60), font=font)
        return im
    return Image.open(p)  # photos; anything else fails here and gets an icon


def save_thumb(im, dest):
    tmp = dest.with_name(f"{dest.stem}.{threading.get_ident()}.tmp")
    try:
        im.draft("RGB", (THUMB, THUMB))  # JPEGs decode straight at small size: much faster
        im = ImageOps.exif_transpose(im).convert("RGBA")
        im.thumbnail((THUMB, THUMB))
        bg = Image.new("RGB", im.size, "white")  # transparent pictures on white, not black
        bg.paste(im, mask=im)
        bg.save(tmp, "JPEG", quality=80)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def walk_files(p):
    if p.is_file():
        yield p
        return
    for d, dirs, files in os.walk(p):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        yield from (Path(d) / f for f in files if not f.startswith("."))


def new_key():
    return "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10))


def hash_pin(pin):
    salt = secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex()


def check_pin(pin, stored):
    salt, digest = stored.split("$")
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex(), digest)


def is_home_ip(addr):
    ip = ipaddress.ip_address(addr)
    return ip.is_private or ip.is_loopback or ip.is_link_local


def device_label(ua):
    kind = next((name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("CrOS", "Chromebook"),
                                        ("Macintosh", "Mac"), ("Windows", "Windows PC"), ("Linux", "Linux PC")) if key in ua), "Device")
    if kind == "Android":
        kind = "Android phone" if "Mobile" in ua else "Android tablet"
    browser = next((name for key, name in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("SamsungBrowser", "Samsung Internet"),
                                           ("Chrome/", "Chrome"), ("Safari/", "Safari")) if key in ua), "")
    return f"{kind} ({browser})" if browser else kind


# --- rich text in notes: keep simple formatting and safe links, drop everything else ---

ALLOWED_TAGS = {"b", "i", "u", "s", "p", "div", "br", "ul", "ol", "li", "blockquote", "code", "pre", "a"}
RENAME_TAGS = {"strong": "b", "em": "i", "strike": "s", "del": "s", "h1": "b", "h2": "b", "h3": "b"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "svg", "math", "template", "noscript", "textarea", "select", "title", "head"}
URL_RE = re.compile(r"https?://[^\s<>\"']*[^\s<>\"'.,;:!?)\]]")


class Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.open, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_WITH_CONTENT:
            self.skip += 1
            return
        tag = RENAME_TAGS.get(tag, tag)
        if self.skip or tag not in ALLOWED_TAGS:
            return
        if tag == "br":
            self.out.append("<br>")
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if not re.match(r"(https?://|mailto:)", href, re.I):
                return
            self.out.append(f'<a href="{html.escape(href)}">')
        else:
            self.out.append(f"<{tag}>")
        self.open.append(tag)

    def handle_endtag(self, tag):
        if tag in DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        tag = RENAME_TAGS.get(tag, tag)
        if self.skip or tag not in self.open:
            return
        while self.open:
            t = self.open.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if self.skip:
            return
        if "a" in self.open:
            return self.out.append(html.escape(data))
        pos = 0
        for m in URL_RE.finditer(data):  # bare web addresses become links
            self.out.append(html.escape(data[pos:m.start()]))
            self.out.append(f'<a href="{html.escape(m.group())}">{html.escape(m.group())}</a>')
            pos = m.end()
        self.out.append(html.escape(data[pos:]))


def clean_html(s):
    c = Cleaner()
    c.feed(s)
    c.close()
    return "".join(c.out + [f"</{t}>" for t in reversed(c.open)])


# --- Windows Firewall: blocks other devices by default (always, on "Public" Wi-Fi) ---

def firewall_ok():
    if os.name != "nt":
        return True
    r = subprocess.run("netsh advfirewall firewall show rule name=HomeShare verbose", capture_output=True, creationflags=NO_WINDOW)
    out = r.stdout.decode(errors="replace").lower()
    # shortcut: plain path match, so a non-ASCII install path asks again each start; parse netsh output if that matters
    return r.returncode == 0 and sys.executable.lower() in out and "localsubnet" in out


_fw = [0.0, False]


def firewall_ok_cached():  # netsh takes a moment; the page asks often
    if time.time() - _fw[0] > 20:
        _fw[:] = [time.time(), firewall_ok()]
    return _fw[1]


def firewall_allow():
    """Relaunch ourselves as admin (Windows asks once: "Allow HomeShare to make changes?") to add the rule."""
    if os.name != "nt":
        return
    _fw[0] = 0
    params = "--allow-firewall" if getattr(sys, "frozen", False) else f'"{Path(__file__).resolve()}" --allow-firewall'
    if ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 0) <= 32:
        return  # they clicked No; the page offers it again
    for _ in range(30):
        if firewall_ok():
            return
        time.sleep(0.5)


def firewall_allow_now():  # runs in the elevated copy
    prog = sys.executable
    cmds = ["delete rule name=HomeShare",  # only devices on the same local network, never the wider internet
            f'add rule name=HomeShare dir=in action=allow program="{prog}" profile=any remoteip=localsubnet enable=yes']
    if getattr(sys, "frozen", False):  # remove any "block" rules Windows made for HomeShare.exe; they'd override ours
        cmds.insert(0, f'delete rule name=all dir=in program="{prog}"')
    for c in cmds:
        subprocess.run(f"netsh advfirewall firewall {c}", capture_output=True, creationflags=NO_WINDOW)


def network_name(ip):
    """The Wi-Fi / network name this computer is on (Windows), or None if unknown."""
    if os.name != "nt" or ip.startswith("127."):
        return None  # shortcut: Windows only; elsewhere every network counts as home
    ps = f"(Get-NetConnectionProfile -InterfaceIndex (Get-NetIPAddress -IPAddress '{ipaddress.ip_address(ip)}' -EA Stop).InterfaceIndex -EA Stop).Name"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                           creationflags=NO_WINDOW, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.decode(errors="replace").strip().splitlines()[0] if r.stdout.strip() else None


def check_network(srv):
    srv.ip = lan_ip()  # shortcut: the .local name keeps answering on the old network card until restart
    name = network_name(srv.ip)
    if not name:
        return
    if not srv.trusted:  # the first network HomeShare runs on is home
        srv.trusted.append(name)
        srv.save()
    ok = name in srv.trusted
    if srv.net_ok and not ok:
        srv.log("HomeShare", f'paused sharing: "{name}" isn\'t a home network')
        if srv.notify:
            srv.notify(f'Sharing is paused: "{name}" isn\'t one of your home networks.')
    srv.network, srv.net_ok = name, ok


def watch(srv):
    """Follow Wi-Fi changes every 30 s, and stop sharing when the timer runs out."""
    while True:
        check_network(srv)
        for _ in range(6):
            if srv.stop_at and time.time() > srv.stop_at:
                return srv.shutdown()
            time.sleep(5)


# --- "Start when this computer starts" (Windows, packaged .exe only) ---

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart(on=None):
    """Turn on/off (or just read, with on=None) starting with Windows. None when not available."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return None
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "HomeShare", 0, winreg.REG_SZ, f'"{sys.executable}" --background')
        elif on is False:
            try:
                winreg.DeleteValue(k, "HomeShare")
            except FileNotFoundError:
                pass
        try:
            winreg.QueryValueEx(k, "HomeShare")
            return True
        except FileNotFoundError:
            return False


PICK_LOCK = threading.Lock()


def pick_folder(start):
    """Show a folder picker on the sharing computer's screen."""
    import tkinter, tkinter.filedialog
    with PICK_LOCK:
        tk = tkinter.Tk()
        tk.withdraw()
        tk.attributes("-topmost", True)
        try:
            return tkinter.filedialog.askdirectory(parent=tk, initialdir=start, title="Choose the folder to share")
        finally:
            tk.destroy()


def clean_name(s):
    # letters, digits and dashes only, so it works as a "<name>.local" address
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", s.lower())).strip("-")[:40]


def clean_filename(s):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # sends nothing; just asks the OS which network card is the LAN one
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def unique(p):
    stem, suffix, i = p.stem, p.suffix, 1
    while p.exists():
        p = p.with_name(f"{stem} ({i}){suffix}")
        i += 1
    return p


# --- mDNS: answers "who is <name>.local?" so people can use a friendly link instead of an IP ---

def read_name(pkt, off):
    parts, end = [], None
    for _ in range(64):  # bounded: a malicious packet can contain pointer loops
        n = pkt[off]
        if n >= 0xC0:
            end = end or off + 2
            off = ((n & 0x3F) << 8) | pkt[off + 1]
            continue
        off += 1
        if n == 0:
            return ".".join(parts), end or off
        parts.append(pkt[off:off + n].decode())
        off += n
    raise ValueError("name too long")


def mdns_reply(pkt, host, ip):
    """Return an mDNS answer packet if pkt asks for host.local, else None."""
    try:
        qid, flags, qd = struct.unpack("!HHH", pkt[:6])
        if flags & 0x8000:  # a response from someone else, not a question
            return None
        off, wanted = 12, False
        for _ in range(qd):
            name, off = read_name(pkt, off)
            qtype = struct.unpack("!H", pkt[off:off + 2])[0]
            off += 4
            wanted |= name.lower() == host + ".local" and qtype in (1, 255)  # A or ANY
    except (struct.error, IndexError, ValueError):
        return None
    if not wanted:
        return None
    rname = bytes([len(host)]) + host.encode() + b"\x05local\x00"
    answer = rname + struct.pack("!HHIH", 1, 0x8001, 120, 4) + socket.inet_aton(ip)
    return struct.pack("!6H", qid, 0x8400, 0, 1, 0, 0) + answer


def mdns_loop(srv):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):  # macOS/Linux already run an mDNS service on this port
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", 5353))
        group = socket.inet_aton("224.0.0.251")
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group + socket.inet_aton(srv.ip))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(srv.ip))
    except OSError as e:
        print(f"(The friendly link is unavailable: {e}. The number link still works.)")
        return
    while True:
        try:
            pkt, addr = s.recvfrom(9000)
            r = mdns_reply(pkt, srv.name, srv.ip)
            if r and srv.net_ok:
                s.sendto(r, ("224.0.0.251", 5353) if addr[1] == 5353 else addr)
        except OSError:
            time.sleep(1)


# --- web server ---

class Handler(BaseHTTPRequestHandler):
    server_version = "HomeShare/3"
    protocol_version = "HTTP/1.1"
    body_done = True
    gate = None
    timeout = 60  # a phone that walks away mid-transfer frees its connection instead of holding it forever

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def is_host(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def cookie(self, key):
        try:
            c = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        return c[key].value if key in c else ""

    def dev_id(self):
        v = self.cookie("hs_dev")
        return v if re.fullmatch(r"[0-9a-f]{16}", v) else None

    def dev_cookie(self):
        if self.is_host() or self.dev_id():
            return []
        return [("Set-Cookie", f"hs_dev={secrets.token_hex(8)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000")]

    def me(self):
        """(device id, record) of whoever is asking."""
        if self.is_host():
            return "host", {"name": self.server.host_name, "label": "Sharing computer"}
        did = self.dev_id()
        d = self.server.devices.get(did) if did else None
        return did, d or {"name": "", "label": device_label(self.headers.get("User-Agent", ""))}

    def who(self):
        d = self.me()[1]
        return d["name"] or d["label"]

    def rel(self, p):
        return p.relative_to(self.server.root).as_posix()

    def check_gate(self):
        """What's stopping this device getting in (None = nothing): link, pin, blocked, approval or paused."""
        srv = self.server
        if not srv.net_ok:
            return "paused"
        if srv.require_key and not hmac.compare_digest(self.cookie("hs_key").encode(), srv.key.encode()):
            return "link"
        if srv.pin_hash and not hmac.compare_digest(self.cookie("hs_pin").encode(), srv.pin_cookie().encode()):
            return "pin"
        did = self.dev_id()
        d = srv.touch(did, self.headers.get("User-Agent", ""), self.client_address[0]) if did else None
        if d and d["status"] == "blocked":
            return "blocked"
        if srv.approve and (not d or d["status"] != "allowed"):
            return "approval"
        return None

    def send(self, code, body=b"", ctype="application/json", headers=()):
        if not self.body_done:  # answering an upload early: read the rest so the connection stays usable
            self.body_done = True
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_DRAIN or copy_n(self.rfile, None, n):
                self.close_connection = True
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        if ctype == "application/json":
            headers = [("Cache-Control", "no-store"), *headers]  # iPhone Safari otherwise may show a stale file list
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, p, how="inline", ctype=None, extra=()):
        ctype = ctype or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        with p.open("rb") as f:
            size = os.fstat(f.fileno()).st_size
            start, end = 0, size - 1
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            partial = bool(m and any(m.groups()))  # videos ask for pieces so people can skip around
            if partial:
                a, b = m.groups()
                start, end = (int(a), min(int(b or end), end)) if a else (max(0, size - int(b)), end)
                if a and not b:  # "from here to the end": send one piece; the browser asks for the next when it needs it
                    end = min(end, start + RANGE_PIECE - 1)
                if start > end:
                    return self.send(416, headers=[("Content-Range", f"bytes */{size}")])
            self.send_response(206 if partial else 200)
            hdrs = [("Content-Type", ctype), ("Content-Length", str(end - start + 1)), ("Accept-Ranges", "bytes"),
                    ("Content-Disposition", f"{how}; filename*=UTF-8''{quote(p.name)}"), *extra]
            if partial:
                hdrs.append(("Content-Range", f"bytes {start}-{end}/{size}"))
            if ctype in SCRIPTABLE:
                hdrs.append(("Content-Security-Policy", "sandbox"))  # shared web pages can't run scripts
            for k, v in hdrs:
                self.send_header(k, v)
            self.end_headers()
            f.seek(start)
            copy_n(f, self.wfile, end - start + 1)

    def local(self, rel):
        """Map a path from the browser to a real path, refusing anything outside the shared folder."""
        root = self.server.root
        p = (root / rel.lstrip("/\\")).resolve()
        if p != root and root not in p.parents:
            raise PermissionError(rel)
        return p

    def new_path(self, rel, parents=False):
        parent, _, name = rel.replace("\\", "/").strip("/").rpartition("/")
        name = clean_filename(name)
        if not name:
            raise ValueError("Please choose a name.")
        if parents:  # uploading a whole folder: create its folders, with safe names
            parent = "/".join(clean_filename(x) or "_" for x in parent.split("/") if x)
        parent = self.local(parent)
        if parents:
            parent.mkdir(parents=True, exist_ok=True)
        if not parent.is_dir():
            raise FileNotFoundError(rel)
        return parent / name

    def route(self, fn):
        srv, u = self.server, urlsplit(self.path)
        h = self.headers.get("Host", "").lower()
        # Only answer to our own addresses: stops other websites reaching this via DNS tricks
        if (h if h.endswith("]") else h.rsplit(":", 1)[0]) not in srv.hosts():
            return self.send(403, {"error": "Unknown address."})
        if not self.is_host():
            if not is_home_ip(self.client_address[0]):  # second line of defence behind the firewall rule
                return self.send(403, b"HomeShare only works inside a home network.", "text/plain; charset=utf-8")
            if not srv.lan:
                return self.send(403, b"Sharing is turned off on the sharing computer.", "text/plain; charset=utf-8")
            self.gate = self.check_gate()
            if self.gate and u.path not in OPEN_PATHS and not code_from_path(u.path) \
                    and not (self.gate == "approval" and u.path == "/api/me"):
                return self.send(401, {"need": self.gate, "error": NEED_MSG[self.gate]})
            if u.path in HOST_PATHS:
                return self.send(403, {"error": "Only the sharing computer can change settings."})
            if (u.path in EDIT_PATHS or self.command == "PUT") and self.command != "GET" and not srv.guest_edit:
                return self.send(403, {"error": "The sharing computer allows viewing and downloading only."})
        try:
            fn(u)
        except Refused as e:
            self.send(403, {"error": str(e)})
        except FileNotFoundError:
            self.send(404, {"error": "That file or folder no longer exists."})
        except FileExistsError:
            self.send(409, {"error": "Something with that name is already there."})
        except PermissionError:
            self.send(403, {"error": "That isn't allowed."})
        except (ConnectionError, TimeoutError):
            self.close_connection = True  # the visitor closed the page mid-transfer
        except (OSError, ValueError, KeyError, TypeError, shutil.Error) as e:
            self.send(400, {"error": str(e) or "Something went wrong."})

    def do_GET(self):
        self.route(self.get)

    def do_PUT(self):
        self.body_done = False
        self.route(self.put)

    def do_POST(self):
        self.route(self.post)

    def get(self, u):
        srv, q = self.server, parse_qs(u.query)
        if u.path == "/":
            nonce = secrets.token_urlsafe(16)  # only our own script runs, even if a note somehow carried one
            return self.send(200, PAGE.replace("__NONCE__", nonce).encode(), "text/html; charset=utf-8", headers=[
                ("Content-Security-Policy", PAGE_CSP.format(nonce=nonce)), ("X-Frame-Options", "DENY"),
                ("Cache-Control", "no-store"), *self.dev_cookie()])
        code = code_from_path(u.path)
        if code:  # the share link / QR code: remember it on this device, then show the page
            hdrs = [("Location", "/"), *self.dev_cookie()]
            fails = srv.code_fails.setdefault(self.client_address[0], [0, 0.0])
            if time.time() < fails[1]:
                return self.send(302, headers=hdrs)  # too many wrong guesses lately: nothing gets remembered
            if hmac.compare_digest(code.encode(), srv.key.encode()):
                hdrs.append(("Set-Cookie", f"hs_key={srv.key}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"))
                p = q.get("p", [""])[0]
                if p and srv.pin_hash and srv.qr_pin and hmac.compare_digest(p.encode(), srv.pin_cookie()[:16].encode()):
                    hdrs.append(("Set-Cookie", f"hs_pin={srv.pin_cookie()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"))
            elif srv.require_key and not self.is_host():  # custom codes can be short: stop anyone guessing through them
                fails[0] += 1
                if fails[0] >= 10:
                    fails[:] = [0, time.time() + 900]
                    srv.log(self.who(), "tried 10 wrong links and is locked out for 15 minutes")
            return self.send(302, headers=hdrs)
        if u.path == "/api/info":
            did, d = self.me()
            return self.send(200, srv.info(self.is_host(), self.gate, {"id": did, "name": d["name"], "label": d["label"]}))
        if u.path == "/api/list":
            d = self.local(q.get("dir", [""])[0])
            items = []
            for e in os.scandir(d):
                if e.name.startswith("."):
                    continue
                try:
                    st = e.stat()
                    it = {"n": e.name, "d": e.is_dir(), "s": st.st_size, "m": int(st.st_mtime)}
                    custom = srv.custom_thumb(self.rel(d / e.name))
                    if not it["d"] and custom.exists():
                        it["t"] = int(custom.stat().st_mtime)  # someone picked this file's thumbnail
                    items.append(it)
                except OSError:
                    pass
            return self.send(200, {"items": items})
        if u.path == "/api/notes":
            return self.send(200, {"posts": srv.notes, "me": self.me()[0]})
        if u.path == "/api/devices":
            now = time.time()
            devs = [{"id": k, **v, "online": v["status"] == "allowed" and now - v.get("seen", 0) < 90} for k, v in srv.devices.items()]
            devs.sort(key=lambda d: (d["status"] != "pending", not d["online"], -d.get("seen", 0)))
            return self.send(200, {"devices": devs})
        if u.path == "/api/live":  # held open by every page: when HomeShare stops, even from Task Manager, Windows cuts
            self.send_response(200)  # it at once, so pages know straight away instead of waiting for a check to time out
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.close_connection = True
            host = self.is_host()
            srv.host_tabs += host  # a HomeShare tab open on this computer: no need to open another at startup
            try:
                self.wfile.write(b"retry: 2000\n\n")  # if it drops, the browser tries again every 2 s
                while True:  # a tiny note every 15 s keeps routers from dropping the quiet connection
                    self.wfile.write(b": here\n\n")
                    self.wfile.flush()
                    time.sleep(15)
            finally:
                srv.host_tabs -= host
        if u.path == "/api/activity":
            items = []
            for e in srv.activity:  # for each file mentioned: is it still there, or (if removed) still in the trash?
                e = dict(e)
                if "path" in e:
                    e["exists"] = (srv.root / e["path"]).exists()
                    if "removed" in e:
                        e["in_trash"] = (srv.root / TRASH / e["removed"]).exists()
                items.append(e)
            return self.send(200, {"items": items})
        if u.path == "/api/speedtest":  # throwaway data for the speed test, the other way
            n = min(int(q.get("mb", ["32"])[0]), 256) << 20
            self.send_response(200)
            for k, v in (("Content-Type", "application/octet-stream"), ("Content-Length", str(n)), ("Cache-Control", "no-store")):
                self.send_header(k, v)
            self.end_headers()
            chunk = bytes(1 << 20)
            for _ in range(n >> 20):
                self.wfile.write(chunk)
            return
        if u.path == "/api/qr.svg" and segno:  # the number link: phones can't always open .local names
            buf = io.BytesIO()
            segno.make(srv.qr_url()).save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff", xmldecl=False)
            return self.send(200, buf.getvalue(), "image/svg+xml", headers=[("Cache-Control", "no-store")])
        if u.path.startswith("/f/"):
            p = self.local(unquote(u.path[3:]))
            if "dl" in q and not self.headers.get("Range"):
                srv.log(self.who(), f"downloaded {self.rel(p)}", path=self.rel(p))
            return self.send_file(p, "attachment" if "dl" in q else "inline")
        if u.path.startswith("/thumb/"):
            return self.thumb(self.local(unquote(u.path[7:])))
        self.send(404, {"error": "Not found."})

    def thumb(self, p):
        cache = [("Cache-Control", "max-age=604800")]  # safe: the page's thumbnail links change when the file does
        custom = self.server.custom_thumb(self.rel(p))
        if custom.exists():
            return self.send_file(custom, ctype="image/jpeg", extra=cache)
        no_preview = lambda: self.send(415, b"No preview.", "text/plain", headers=cache)  # the page shows an icon instead
        if Image is None:  # without Pillow only photos get a thumbnail: the browser shrinks the original
            return self.send_file(p) if (mimetypes.guess_type(p.name)[0] or "").startswith("image/") else no_preview()
        if p.suffix.lower() == ".svg":
            return self.send_file(p)
        st = p.stat()
        t = self.server.root / THUMBS / (hashlib.sha1(f"{p}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest() + ".jpg")
        none = t.with_suffix(".none")  # remembered "nothing to show", so it isn't worked out again each time
        if none.exists():
            return no_preview()
        if not t.exists():  # shortcut: cache is never pruned; delete .thumbs if it grows too big
            t.parent.mkdir(exist_ok=True)
            try:
                im = preview_image(p)
                if im is None:
                    raise ValueError("nothing to show")
                with im:
                    save_thumb(im, t)
            except Exception:  # e.g. a video, iPhone HEIC photo, or a document without a saved preview
                none.touch()
                return no_preview()
        self.send_file(t, ctype="image/jpeg", extra=cache)

    def set_thumb(self, u):
        """PUT /thumb/<path> with a picture: use it as this file's thumbnail on every device."""
        target = self.local(unquote(u.path[7:]))
        if not target.is_file():
            raise FileNotFoundError(u.path)
        n = int(self.headers.get("Content-Length", -1))
        if not 0 < n <= 30 << 20:
            raise Refused("Please pick a picture smaller than 30 MB.")
        if Image is None:
            raise Refused("Custom thumbnails need the full HomeShare app.")
        data = self.rfile.read(n)
        self.body_done = True
        dest = self.server.custom_thumb(self.rel(target))
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(io.BytesIO(data)) as im:
                save_thumb(im, dest)
        except Exception:
            raise Refused("That picture couldn't be used. Please try a JPEG or PNG.")
        self.server.log(self.who(), f"changed the thumbnail of {self.rel(target)}", path=self.rel(target))
        self.send(201, {"ok": True})

    def put(self, u):
        """Upload: PUT /f/<path> with the whole file as the body (one request per file: what works on every phone)."""
        if u.path.startswith("/thumb/"):
            return self.set_thumb(u)
        if u.path == "/api/speedtest":  # throwaway data for the speed test: read it as fast as it comes, keep nothing
            n, t0 = min(int(self.headers.get("Content-Length", 0)), 256 << 20), time.time()
            self.body_done = True
            if copy_n(self.rfile, None, n):
                raise ConnectionError("speed test cut off")
            return self.send(200, {"secs": time.time() - t0})
        if not u.path.startswith("/f/"):
            return self.send(404, {"error": "Not found."})
        dest = self.new_path(unquote(u.path[3:]), parents=True)
        n = int(self.headers.get("Content-Length", -1))
        if n < 0:
            raise ValueError("Missing file size.")
        if shutil.disk_usage(self.server.root).free < n + RESERVE:
            raise Refused("The sharing computer is almost out of space, so it isn't taking uploads right now.")
        tmp = dest.with_name(f".{dest.name}.{threading.get_ident()}.part")  # hidden until complete
        t0 = time.time()
        try:
            with tmp.open("wb") as f:
                self.body_done = True
                try:
                    copy_n(self.rfile, f, n)
                except (ConnectionError, TimeoutError):
                    pass
                got = f.tell()
            if got < n:  # the device stopped sending: say so where the sharing computer can see it
                self.server.log(self.who(), f"stopped sending {dest.name} partway ({got / 1048576:.1f} of {n / 1048576:.1f} MB arrived)")
                raise ConnectionError("upload cut off")
            with LOCK:
                dest = unique(dest)  # never overwrite: a second "photo.jpg" becomes "photo (1).jpg"
                os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        secs = max(time.time() - t0, 0.001)
        speed = f" ({n / 1048576:.0f} MB in {secs:.0f} s, {n / 1048576 / secs:.1f} MB/s)" if n >= 10 << 20 else ""  # big files: how fast it came
        self.server.log(self.who(), f"uploaded {self.rel(dest)}{speed}", path=self.rel(dest))
        if self.server.on_upload and not self.is_host():
            self.server.on_upload(dest.name)
        self.send(201, {"path": self.rel(dest)})

    def send_zip(self, form):
        rel = form.get("dir", [""])[0]
        base = self.local(rel)
        paths = [self.local(f"{rel}/{n}") for n in form.get("n", [])]
        if not paths:
            raise ValueError("Nothing selected.")
        if any(p.parent != base for p in paths):
            raise PermissionError(rel)
        name = (paths[0].name if len(paths) == 1 else base.name if base != self.server.root else "HomeShare") + ".zip"
        self.server.log(self.who(), f"downloaded {len(paths)} item(s) as {name}")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True  # size isn't known up front: the download ends when the connection closes
        with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED) as z:  # photos and videos are already compressed
            for p in paths:
                for f in walk_files(p):
                    try:
                        z.write(f, f.relative_to(base).as_posix())
                    except OSError:
                        pass  # skip files that vanished or are locked rather than break the whole zip

    def login(self, b):
        srv, ip = self.server, self.client_address[0]
        fails = srv.fails.setdefault(ip, [0, 0.0])
        if time.time() < fails[1]:
            raise Refused("Too many wrong PINs. Try again in 15 minutes.")
        if srv.pin_hash and not check_pin(str(b.get("pin", "")).strip(), srv.pin_hash):
            fails[0] += 1
            if fails[0] >= 5:
                fails[:] = [0, time.time() + 900]
                srv.log(self.who(), "entered a wrong PIN 5 times and is locked out for 15 minutes")
            with LOGIN_LOCK:
                time.sleep(1)  # one guess per second at most
            raise Refused("That PIN isn't right.")
        srv.fails.pop(ip, None)
        cookie = f"hs_pin={srv.pin_cookie()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
        self.send(200, {"ok": True}, headers=[("Set-Cookie", cookie)])

    def post_note(self, b):
        srv, root = self.server, self.server.root
        body = str(b.get("html", ""))
        if len(body) > 100_000:
            raise Refused("That post is too long.")
        body = clean_html(body)
        files = []
        for rel in b.get("files", [])[:20]:
            f = self.local(rel)
            if f.is_file():
                files.append({"path": self.rel(f), "n": f.name, "s": f.stat().st_size})
        if not re.sub(r"<[^>]*>", "", body).strip() and not files:
            raise Refused("Write something or attach a file first.")
        parent = b.get("parent") or None
        if parent and not any(p["id"] == parent and not p["parent"] for p in srv.notes):
            raise FileNotFoundError(parent)
        did, d = self.me()
        post = {"id": secrets.token_hex(6), "parent": parent, "dev": did, "name": d["name"] or d["label"], "label": d["label"],
                "t": int(time.time()), "html": body, "files": files}
        with NOTES_LOCK:
            srv.notes.append(post)
            srv.save_notes()
        srv.log(post["name"], "replied in Notes" if parent else "started a thread in Notes")

    def post(self, u):
        srv, root = self.server, self.server.root
        n = int(self.headers.get("Content-Length", 0))
        if n > 1 << 20:
            raise ValueError("Request too large.")
        body = self.rfile.read(n)
        if u.path == "/zip":  # a normal form post, so the browser handles the download itself
            return self.send_zip(parse_qs(body.decode()))
        # JSON-only from here: browsers won't let other websites send this type without asking us first
        if self.headers.get("Content-Type") != "application/json":
            raise PermissionError("bad content type")
        b = json.loads(body or b"{}")
        who = self.who()
        if u.path == "/api/login":
            return self.login(b)
        if u.path == "/api/mkdir":
            p = self.new_path(b["path"])
            p.mkdir()
            srv.log(who, f"made the folder {self.rel(p)}", path=self.rel(p))
        elif u.path == "/api/move":
            src, dst = self.local(b["path"]), self.new_path(b["to"])
            if src == root:
                raise PermissionError("root")
            if src in dst.parents:
                raise Refused("A folder can't go inside itself.")
            with LOCK:
                if dst.exists() and dst != src:
                    raise FileExistsError(dst)
                shutil.move(src, dst)
                custom = srv.custom_thumb(self.rel(src))
                if custom.exists():  # a chosen thumbnail follows its file (shortcut: not for files inside a moved folder)
                    os.replace(custom, srv.custom_thumb(self.rel(dst)))
            verb = "renamed" if src.parent == dst.parent else "moved"
            srv.log(who, f"{verb} {self.rel(src)} to {self.rel(dst)}", path=self.rel(dst))
        elif u.path == "/api/thumbreset":
            srv.custom_thumb(self.rel(self.local(b["path"]))).unlink(missing_ok=True)
        elif u.path == "/api/delete":
            src = self.local(b["path"])
            if src == root:
                raise PermissionError("root")
            (root / TRASH).mkdir(exist_ok=True)
            with LOCK:
                binned = unique(root / TRASH / src.name)
                shutil.move(src, binned)  # recoverable, not gone
            srv.log(who, f"deleted {self.rel(src)}", path=self.rel(src), removed=binned.name)
        elif u.path == "/api/notes":
            self.post_note(b)
        elif u.path == "/api/notes/delete":
            did = self.me()[0]
            with NOTES_LOCK:
                post = next((p for p in srv.notes if p["id"] == b["id"]), None)
                if not post:
                    raise FileNotFoundError(b["id"])
                if not self.is_host() and post["dev"] != did:
                    raise Refused("You can only delete your own posts.")
                srv.notes = [p for p in srv.notes if p["id"] != post["id"] and p["parent"] != post["id"]]
                srv.save_notes()
        elif u.path == "/api/me":
            name = str(b["name"]).strip()[:40]
            if not name:
                raise Refused("Please type a name.")
            did = self.me()[0]
            if did == "host":
                srv.host_name = name
            elif did in srv.devices:
                srv.devices[did]["name"] = name
            else:
                raise Refused("Open the page again, then try.")
            srv.save()
        elif u.path == "/api/restore":
            src = self.local(f"{TRASH}/{b['name']}")
            if src.parent != root / TRASH:
                raise Refused("That isn't in the trash.")
            with LOCK:
                dest = unique(root / src.name)
                shutil.move(src, dest)
            srv.log(who, f"restored {src.name} from the trash", path=self.rel(dest))
        elif u.path == "/api/emptytrash":
            shutil.rmtree(root / TRASH, ignore_errors=True)
            srv.log(who, "emptied the trash")
        elif u.path == "/api/activity/clear":
            srv.activity.clear()
        elif u.path in ("/api/activity/remove", "/api/activity/reclaim"):  # from Recent activity: the log line stays
            e = next((x for x in srv.activity if x["id"] == b["id"] and "path" in x), None)
            if not e:
                raise FileNotFoundError(b["id"])
            if u.path.endswith("remove"):
                src = self.local(e["path"])
                if src == root or not src.exists():
                    raise FileNotFoundError(e["path"])
                (root / TRASH).mkdir(exist_ok=True)
                with LOCK:
                    binned = unique(root / TRASH / src.name)
                    shutil.move(src, binned)  # into the trash, so it can be reclaimed
                e["removed"] = binned.name
            else:
                src = root / TRASH / e.get("removed", "")
                if "removed" not in e or not src.exists():
                    raise Refused("That file isn't in the trash any more (it was restored already, or the trash was emptied).")
                with LOCK:
                    dest = unique(self.new_path(e["path"], parents=True))  # back where it was, beside anything new there
                    shutil.move(src, dest)
                e["path"] = self.rel(dest)
                del e["removed"]
        elif u.path == "/api/link":  # http://<name>.local/<code>; no code = anyone on the Wi-Fi can open it
            if "name" in b:
                name = clean_name(b["name"])
                if not name:
                    raise Refused("Use letters, numbers or dashes for the name.")
                srv.name = name
            if "code" in b:
                code = clean_code(b["code"])
                if code in RESERVED_CODES:
                    raise Refused(f'"{code}" is used by HomeShare itself. Please pick another code.')
                if code and code != srv.key:
                    srv.key = code
                srv.require_key = bool(code)
            srv.save()
        elif u.path == "/api/settings":
            for k in ("lan", "guest_edit", "require_key", "approve", "qr_pin"):
                if k in b:
                    setattr(srv, k, bool(b[k]))
            if "pin" in b:
                pin = str(b["pin"]).strip()[:64]
                srv.pin_hash = hash_pin(pin) if pin else ""
            if "stop_hours" in b:
                srv.set_stop(float(b["stop_hours"]))
            if "autostart" in b:
                autostart(bool(b["autostart"]))
            srv.save()
        elif u.path == "/api/device":
            d = srv.devices.get(b["id"])
            if not d:
                raise FileNotFoundError(b["id"])
            label = d["name"] or d["label"]
            if b["action"] == "forget":
                del srv.devices[b["id"]]
            else:
                d["status"] = {"allow": "allowed", "block": "blocked"}[b["action"]]
            srv.save()
            srv.log(who, {"allow": "let in", "block": "removed", "forget": "forgot"}[b["action"]] + " " + label)
        elif u.path == "/api/trust":
            name = str(b["name"])
            if b["on"] and name not in srv.trusted:
                srv.trusted.append(name)
            elif not b["on"] and name in srv.trusted:
                srv.trusted.remove(name)
            srv.net_ok = srv.network is None or srv.network in srv.trusted
            srv.save()
        elif u.path == "/api/newkey":
            srv.key, srv.require_key = new_key(), True
            srv.save()
            srv.log(who, "made a new link (the old one stopped working)")
        elif u.path == "/api/firewall":
            firewall_allow()
        elif u.path == "/api/open":
            os.startfile(root) if os.name == "nt" else webbrowser.open(root.as_uri())
        elif u.path == "/api/folder":
            picked = pick_folder(str(root))
            if picked:
                srv.root = Path(picked).resolve()
                srv.load_notes()
                save_cfg(folder=str(srv.root))
        elif u.path == "/api/stop":
            threading.Thread(target=srv.shutdown).start()
        else:
            return self.send(404, {"error": "Not found."})
        self.send(200, {"ok": True})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows this would let us silently share a port another app owns

    def __init__(self, root, name, port, cfg=None):
        super().__init__(("", port), Handler)
        cfg = cfg or {}
        self.root, self.name, self.ip = Path(root).resolve(), name, lan_ip()
        self.lan, self.guest_edit = cfg.get("lan", True), cfg.get("guest_edit", True)
        self.secret, self.key = cfg.get("secret") or secrets.token_hex(16), cfg.get("key") or new_key()
        self.require_key, self.approve, self.qr_pin = cfg.get("require_key", False), cfg.get("approve", False), cfg.get("qr_pin", True)
        self.pin_hash = cfg.get("pin_hash") or (hash_pin(cfg["pin"]) if cfg.get("pin") else "")  # older versions kept it as plain text
        self.trusted, self.devices = cfg.get("trusted", []), cfg.get("devices", {})
        self.host_name = cfg.get("host_name") or socket.gethostname()
        self.network, self.net_ok = None, True
        self.activity, self.fails, self.code_fails = deque(maxlen=500), {}, {}  # shortcut: the activity log is forgotten on restart
        self.on_upload = self.notify = None
        self.host_tabs = 0
        self.log_ids = itertools.count(1)
        self.set_stop(cfg.get("stop_hours", STOP_DEFAULT))
        self.load_notes()
        self.save()

    def save(self):
        save_cfg(name=self.name, lan=self.lan, guest_edit=self.guest_edit, stop_hours=self.stop_hours, secret=self.secret,
                 key=self.key, require_key=self.require_key, approve=self.approve, qr_pin=self.qr_pin, pin_hash=self.pin_hash,
                 pin=None, trusted=self.trusted, devices=self.devices, host_name=self.host_name)

    def load_notes(self):
        try:
            self.notes = json.loads((self.root / NOTES_FILE).read_text("utf-8"))
        except (OSError, ValueError):
            self.notes = []

    def save_notes(self):
        tmp = self.root / (NOTES_FILE + ".tmp")
        tmp.write_text(json.dumps(self.notes), "utf-8")
        os.replace(tmp, self.root / NOTES_FILE)

    def custom_thumb(self, rel):
        """Where a thumbnail someone picked for this file is kept (keyed by its path, any letter case)."""
        return self.root / THUMBS / "custom" / (hashlib.sha1(rel.lower().encode()).hexdigest() + ".jpg")

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):  # phones hang up mid-request all the time
            super().handle_error(request, client_address)

    def set_stop(self, hours):
        self.stop_hours = hours
        self.stop_at = time.time() + hours * 3600 if hours else None

    def pin_cookie(self):
        return hmac.new(bytes.fromhex(self.secret), self.pin_hash.encode(), "sha256").hexdigest()

    def log(self, who, what, path=None, removed=None):
        """Add to Recent activity. Entries about a file remember it (path, size, folder or not), so the sharing computer
        can remove that file from the log, or reclaim it; `removed` = its name in the trash."""
        e = {"id": next(self.log_ids), "t": int(time.time()), "who": who, "what": what}
        if path:
            f = self.root / TRASH / removed if removed else self.root / path
            e.update(path=path, dir=f.is_dir(), size=f.stat().st_size if f.is_file() else None)
            if removed:
                e["removed"] = removed
        self.activity.appendleft(e)

    def touch(self, did, ua, ip):
        """Record that this device is here; new ones wait for approval if that's switched on."""
        d = self.devices.get(did)
        if d is None:  # shortcut: the device list isn't capped; add pruning if strangers keep clearing cookies
            d = self.devices[did] = {"name": "", "label": device_label(ua), "status": "pending" if self.approve else "allowed",
                                     "first": int(time.time()), "seen": int(time.time()), "ip": ip}
            self.save()
            pending = d["status"] == "pending"
            self.log(d["label"], "asked to connect" if pending else "connected for the first time")
            if pending and self.notify:
                self.notify(f"{d['label']} wants to connect. Open HomeShare to let it in.")
        d["seen"], d["ip"] = int(time.time()), ip
        return d

    def hosts(self):
        return {f"{self.name}.local", self.ip, "127.0.0.1", "localhost", "[::1]", socket.gethostname().lower()}

    def links(self):
        port = self.server_address[1]
        sfx = ("" if port == 80 else f":{port}") + (f"/{self.key}" if self.require_key else "")
        return [f"http://{self.name}.local{sfx}", f"http://{self.ip}{sfx}"]

    def qr_url(self):
        port = self.server_address[1]
        url = f"http://{self.ip}{'' if port == 80 else f':{port}'}/{self.key}"
        return url + (f"?p={self.pin_cookie()[:16]}" if self.pin_hash and self.qr_pin else "")

    def info(self, host, need, me):
        d = {"name": self.host_name, "host": host, "need": need, "edit": host or self.guest_edit, "me": me, "version": VERSION,
             "notes_last": max((p["t"] for p in self.notes), default=0)}
        if host:
            now, trash = time.time(), self.root / TRASH
            d.update(links=self.links(), link_name=self.name, link_code=self.key if self.require_key else "", qr=segno is not None, folder=str(self.root), lan=self.lan,
                     firewall=firewall_ok_cached(), guest_edit=self.guest_edit, stop_hours=self.stop_hours, stop_at=self.stop_at,
                     autostart=autostart(), trash=len(os.listdir(trash)) if trash.is_dir() else 0, network=self.network,
                     net_ok=self.net_ok, trusted=self.trusted, require_key=self.require_key, approve=self.approve,
                     pin_set=bool(self.pin_hash), qr_pin=self.qr_pin, free=shutil.disk_usage(self.root).free,
                     pending=sum(v["status"] == "pending" for v in self.devices.values()),
                     online=sum(v["status"] == "allowed" and now - v.get("seen", 0) < 90 for v in self.devices.values()))
        return d


# --- tray icon (Windows/macOS/Linux desktops, when pystray + Pillow are present) ---

def tray_image(size=64):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d, s = ImageDraw.Draw(im), size / 64
    d.rounded_rectangle((2 * s, 2 * s, 62 * s, 62 * s), 14 * s, fill=(37, 99, 235))
    d.rounded_rectangle((13 * s, 16 * s, 31 * s, 26 * s), 3 * s, fill="white")  # folder tab
    d.rounded_rectangle((13 * s, 21 * s, 51 * s, 48 * s), 4 * s, fill="white")  # folder body
    return im


def run(srv, url):
    if pystray is None or Image is None:
        return srv.serve_forever()
    icon = pystray.Icon("HomeShare", tray_image(), "HomeShare is sharing", pystray.Menu(
        pystray.MenuItem("Open HomeShare", lambda: open_browser(url), default=True),
        pystray.MenuItem("Close HomeShare", lambda: threading.Thread(target=srv.shutdown).start())))
    added, timer, tlock = [], [None], threading.Lock()

    def notify(text):
        try:
            icon.notify(text, "HomeShare")
        except Exception:
            pass

    def flush():  # one notification per burst of uploads, not one per file
        with tlock:
            n, last = len(added), added[-1]
            added.clear()
            timer[0] = None
        notify(f'"{last}" was added.' if n == 1 else f'{n} files were added, including "{last}".')

    def on_upload(name):
        with tlock:
            added.append(name)
            if not timer[0]:
                timer[0] = threading.Timer(3, flush)
                timer[0].start()

    def serve():
        srv.serve_forever()
        icon.stop()

    srv.on_upload, srv.notify = on_upload, notify
    threading.Thread(target=serve, daemon=True).start()
    icon.run()


def running_port():
    """Port of a HomeShare already running on this computer, if any."""
    for port in PORTS:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=1) as r:
                if "links" in json.load(r):
                    return port
        except (OSError, ValueError):
            pass


def open_browser(url):
    """Show the page in the usual browser, started by Windows rather than by HomeShare: otherwise Task Manager files
    the browser under HomeShare, and ending HomeShare there closes the browser too."""
    if os.name == "nt":
        subprocess.Popen(["explorer.exe", url], creationflags=NO_WINDOW)
    else:
        webbrowser.open(url)


def open_unless_tab_waiting(srv, url, wait=4):
    """At startup, reuse the HomeShare tab that's already open: a closed tab keeps trying to reconnect, so if one gets
    through within a few seconds it reloads by itself and no new tab is needed (no pile of old tabs)."""
    for _ in range(wait * 4):
        if srv.host_tabs:
            return
        time.sleep(0.25)
    open_browser(url)


def sharp_windows():
    """Tell Windows HomeShare handles display scaling itself. Otherwise, on a screen scaled above 100%, Windows draws
    its windows (the folder picker, message boxes) small and stretches them, which makes them blurry."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor: sharp on every screen
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()  # older Windows


def main():
    sharp_windows()
    if "--allow-firewall" in sys.argv:
        return firewall_allow_now()
    background = "--background" in sys.argv  # started with Windows: don't pop up a browser
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = running_port()
    if port:  # opened again: just show the page instead of starting a second copy
        if not background:
            open_browser(f"http://127.0.0.1:{port}/")
        return
    cfg = load_cfg()
    root = Path(args[0] if args else cfg.get("folder") or Path.home() / "HomeShare").resolve()
    root.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(root / ".parts", ignore_errors=True)  # half-finished uploads left by the old piece-by-piece uploader
    name = cfg.get("name") or clean_name(socket.gethostname()) or "homeshare"
    if cfg.get("lan", True) and not firewall_ok():
        firewall_allow()  # before listening, so Windows doesn't show its own confusing prompt
    for port in (*PORTS, 0):
        try:
            srv = Server(root, name, port, cfg)
            break
        except OSError:
            pass
    check_network(srv)  # before anyone connects: pause straight away on an unfamiliar network
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    threading.Thread(target=mdns_loop, args=(srv,), daemon=True).start()
    threading.Thread(target=watch, args=(srv,), daemon=True).start()
    print(f"HomeShare is running: {srv.links()[0]}  (sharing {root})")
    if not background:
        threading.Thread(target=open_unless_tab_waiting, args=(srv, url), daemon=True).start()
    try:
        run(srv, url)
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HomeShare</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--fg:#1d2330;--mut:#5f6b7d;--line:#e2e5eb;--acc:#2563eb;--accfg:#fff;--hl:#dbe7fe;--ok:#15803d;--warn:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#13161b;--card:#1c2027;--fg:#e8eaee;--mut:#9aa3b2;--line:#2c323c;--acc:#5b8def;--accfg:#0b1020;--hl:#1e3361;--ok:#4ade80;--warn:#fbbf24}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
header.bar{position:sticky;top:0;z-index:4;background:var(--card);border-bottom:1px solid var(--line)}
.barin{max-width:1100px;margin:0 auto;padding:8px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.brand{font-weight:700;font-size:18px;margin-right:auto}
.tabs{display:flex;gap:4px;overflow-x:auto;max-width:100%}
.tabs a{padding:8px 14px;border-radius:8px;color:var(--fg);text-decoration:none;font-weight:600;white-space:nowrap}
.tabs a[aria-current=page]{background:var(--hl);color:var(--acc)}
.dot{color:var(--acc);font-size:11px;vertical-align:super}
main{max-width:1100px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:14px;align-items:start}
.grid2>.card{margin:0}
h2{font-size:16px;margin:0 0 10px}
.mut{color:var(--mut);font-size:13px}
.ok{color:var(--ok)}
.warn{color:var(--warn)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.card>p,.card>form,.card>div{margin:10px 0 0}
.card>h2+*{margin-top:0}
#link{font:600 16px ui-monospace,Consolas,monospace;padding:7px 12px;background:var(--hl);border-radius:8px;word-break:break-all}
figure{margin:12px 0 0;text-align:center}
#qr{width:170px;height:170px;display:block;margin:0 auto;border-radius:8px}
button,.btn,select{font:inherit;font-size:14px;padding:7px 13px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;text-decoration:none;display:inline-block}
button:hover,.btn:hover{border-color:var(--acc)}
button:disabled{opacity:.5;cursor:default}
.pri{background:var(--acc);color:var(--accfg);border-color:var(--acc)}
.danger{border-color:#dc2626;color:#dc2626}
.danger:hover{background:#dc2626;color:#fff;border-color:#dc2626}
input:not([type=checkbox]){font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
input[type=checkbox]{width:18px;height:18px;vertical-align:middle;accent-color:var(--acc)}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.pill{font-size:12px;padding:2px 9px;border-radius:99px;background:var(--hl);white-space:nowrap}
.chip{font-size:13px;padding:3px 6px 3px 10px;border-radius:99px;background:var(--hl);display:inline-flex;gap:4px;align-items:center;margin:2px}
.chip button{border:0;background:none;padding:0 4px;font-size:12px}
.dev,.act{display:flex;gap:10px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line);flex-wrap:wrap}
.dev:last-child,.act:last-child{border-bottom:0}
.dev .who{flex:1;min-width:150px}
.card.wide{grid-column:1/-1}
.act{display:grid;grid-template-columns:80px minmax(0,1fr) 90px 80px 110px}
.act .awhat{overflow-wrap:anywhere}  /* long file names wrap instead of running out of the box */
.act button{padding:3px 9px;font-size:12px;justify-self:end}
.acthead{font-size:12px;font-weight:600;padding-top:0}
@media (max-width:640px){.act{grid-template-columns:64px minmax(0,1fr) auto}.act .acol{display:none}}
body.guest .hostonly,body.ro .edit,.intrash .ntr{display:none!important}
.restore{display:none!important}
.intrash .restore{display:inline-block!important}
nav#crumbs a{color:var(--acc);text-decoration:none;font-weight:600;padding:2px 4px;border-radius:6px}
.tools{margin-top:10px}
#search{flex:1 1 180px;min-width:0}
#selbar{margin-top:10px;padding:8px 10px;background:var(--hl);border-radius:8px}
#list{margin-top:10px}
.list .item{display:grid;grid-template-columns:24px 40px minmax(0,1fr) 80px 170px auto;align-items:center;gap:10px;padding:6px 4px;border-bottom:1px solid var(--line)}
.list .pic{width:40px;height:40px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.grid .item{position:relative;display:flex;flex-direction:column;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--bg)}
.grid .pic{width:100%;aspect-ratio:1;border-radius:0}
.grid .item input{position:absolute;top:8px;left:8px;z-index:1}
.grid .nm{padding:6px 8px;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.grid .sz,.grid .dt,.grid .rowacts,.list .badge{display:none}
.rowacts{display:flex;gap:6px;justify-self:end}
.thb{padding:4px 7px;font-size:13px}
.badge{position:absolute;top:6px;right:6px;font-size:15px;line-height:1;padding:4px 5px;border-radius:6px;background:rgba(0,0,0,.6);pointer-events:none}
.aud{display:flex;flex-direction:column;gap:14px;align-items:center}
.aud img{width:min(70vw,300px);border-radius:10px}
dialog{border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--fg);max-width:min(520px,calc(100% - 32px))}
dialog::backdrop{background:rgba(0,0,0,.5)}
.pic{position:relative;display:flex;align-items:center;justify-content:center;cursor:pointer;overflow:hidden;border-radius:6px;background:var(--hl)}
.pic img,.pic video{width:100%;height:100%;object-fit:cover;pointer-events:none}
.ic{font-size:22px}
.grid .ic{font-size:52px}
.nm{color:var(--fg);text-decoration:none;word-break:break-word}
.nm:hover{text-decoration:underline}
.dl{padding:4px 9px;font-size:13px}
.item.on{background:var(--hl)}
.drop{outline:2px dashed var(--acc);background:var(--hl)!important}
#jobs{position:fixed;right:12px;bottom:12px;width:min(480px,calc(100% - 24px));max-height:45vh;overflow:auto;z-index:6;margin:0;box-shadow:0 6px 24px rgba(0,0,0,.3)}
.chip.warn{background:transparent;border:1px solid var(--warn)}
.job{display:grid;grid-template-columns:minmax(0,1fr) 140px minmax(0,1fr);gap:10px;align-items:center;padding:4px 0}
.job>span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.job button{padding:2px 8px;font-size:12px}
progress{width:100%}
.post{padding:12px 0 6px;border-top:1px solid var(--line)}
.thread>.post:first-child{border-top:0;padding-top:0}
.reply{margin-left:22px}
.phead{display:flex;gap:6px;align-items:baseline;flex-wrap:wrap}
.body{margin:4px 0;word-break:break-word}
.body p,.body div{margin:0}
.body ul,.body ol{margin:4px 0;padding-left:22px}
.body blockquote{border-left:3px solid var(--line);margin:4px 0;padding-left:10px;color:var(--mut)}
.body code,.body pre{font-family:ui-monospace,Consolas,monospace;background:var(--bg);border-radius:4px;padding:0 4px}
.body a{color:var(--acc)}
.embed{margin:6px 0}
.embed img{max-width:min(100%,420px);max-height:320px;border-radius:8px;display:block}
.embed iframe{width:min(100%,480px);aspect-ratio:16/9;border:0;border-radius:8px}
.lcard{display:inline-flex;gap:6px;align-items:baseline;padding:6px 10px;border:1px solid var(--line);border-radius:8px;text-decoration:none;color:var(--fg);font-size:13px;max-width:100%;overflow:hidden}
.atts{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0}
.att{display:flex;gap:6px;align-items:center;border:1px solid var(--line);border-radius:8px;padding:4px 8px;cursor:pointer;background:var(--bg);color:var(--fg);font-size:13px;text-decoration:none;max-width:260px}
.att img{width:64px;height:64px;object-fit:cover;border-radius:6px}
.post .tools button{padding:2px 9px;font-size:12px}
.editor{min-height:70px;max-height:300px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:var(--bg);margin:6px 0}
.editor:empty:before{content:attr(data-ph);color:var(--mut)}
.tb{display:flex;gap:4px;flex-wrap:wrap}
.tb button{padding:3px 9px;font-size:13px}
#drop{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:600;background:rgba(37,99,235,.14);border:4px dashed var(--acc);pointer-events:none;z-index:5}
#viewer{position:fixed;inset:0;background:rgba(0,0,0,.93);z-index:10;display:flex;flex-direction:column}
.vbar{display:flex;gap:8px;align-items:center;padding:10px 12px;color:#fff}
#vname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
#vbody{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;padding:0 8px 8px}
#vbody img,#vbody video{max-width:100%;max-height:100%;object-fit:contain}
#vbody iframe{width:100%;height:100%;border:0;background:#fff;border-radius:6px}
#vbody pre{width:100%;height:100%;overflow:auto;margin:0;padding:14px;border-radius:8px;background:var(--card);color:var(--fg);white-space:pre-wrap;word-break:break-word}
.vnav{position:absolute;top:50%;transform:translateY(-50%);font-size:34px;line-height:1;background:rgba(0,0,0,.45);color:#fff;border:0;width:48px;height:64px}
#vprev{left:6px}
#vnext{right:6px}
@media (max-width:640px){.list .item{grid-template-columns:24px 40px minmax(0,1fr) auto}.list .sz,.list .dt{display:none}
  .job{grid-template-columns:minmax(0,1fr) 90px}.job>span:last-child{grid-column:1/-1}.reply{margin-left:10px}}
</style></head><body>
<header class="bar"><div class="barin">
  <div class="brand">📁 HomeShare <span class="mut" id="sub"></span></div>
  <nav class="tabs" id="tabs" aria-label="Sections">
    <a href="#home" data-t="home" class="hostonly">Home<span class="dot" id="homedot" hidden> ●</span></a>
    <a href="#files" data-t="files">Files</a>
    <a href="#notes" data-t="notes">Notes<span class="dot" id="notesdot" hidden> ●</span></a>
    <a href="#settings" data-t="settings" class="hostonly">Settings</a>
  </nav>
</div></header>
<main>
<div class="card" id="jobs" hidden>
  <div class="row"><strong id="jobsum"></strong><span style="flex:1"></span><button id="jobcancel">Cancel all</button></div>
  <div id="joblist"></div>
</div>

<section id="p-gate" hidden><div class="card" id="gate"></div></section>

<section id="p-home" hidden><div class="grid2">
  <div class="card"><h2>Share</h2>
    <p id="status"></p>
    <div class="row"><span id="link"></span><button class="pri" id="copy">Copy link</button></div>
    <p class="mut" id="alt"></p>
    <figure id="qrbox" hidden><img id="qr" alt="QR code that opens HomeShare"><figcaption class="mut">Scan with a phone camera</figcaption></figure>
  </div>
  <div class="card"><h2>Devices</h2><div id="devs"></div></div>
  <div class="card wide"><div class="row" style="margin:0 0 10px"><h2 style="margin:0">Recent activity</h2><span style="flex:1"></span><button id="actclear">Clear recent activity</button></div>
    <div id="acts"></div><p><button id="actmore">Show more</button></p></div>
  <div class="card"><h2>This computer</h2><p id="hfolder"></p><p class="mut" id="hdisk"></p><p class="row"><button id="openf2">Open folder</button><button id="chf2">Change folder…</button></p>
    <p class="row"><button class="danger" id="close2">Close HomeShare</button><span class="mut">Stops sharing and closes the program. Your browser stays open.</span></p>
    <p class="mut" id="hver"></p></div>
</div></section>

<section id="p-files" hidden><div class="card">
  <nav id="crumbs" aria-label="Folder"></nav>
  <div class="row tools">
    <input type="checkbox" id="selall" aria-label="Select everything shown">
    <input id="search" type="search" placeholder="Search this folder" aria-label="Search this folder">
    <select id="sortk" aria-label="Sort by"><option value="n">Name</option><option value="m">Date</option><option value="s">Size</option></select>
    <button id="sortd" aria-label="Reverse the order">↑</button>
    <button id="viewt">Grid view</button>
    <span class="edit ntr row"><button id="mk">New folder</button><button id="upf">Upload folder</button><button class="pri" id="up">Upload files</button></span>
  </div>
  <input type="file" id="pick" multiple hidden><input type="file" id="pickd" webkitdirectory hidden>
  <div class="row" id="selbar" hidden>
    <strong id="selcount"></strong>
    <button id="seldl" class="ntr">Download</button>
    <span class="edit ntr row"><button id="selren">Rename</button><button id="selthumb">Thumbnail</button><button id="selmv">Move</button><button id="seldel">Delete</button></span>
    <button id="selres" class="restore">Restore</button>
    <button id="selclr">Clear selection</button>
  </div>
  <p class="mut" id="msg" aria-live="polite"></p>
  <div id="list" class="list"></div>
  <p class="mut" id="empty" hidden></p>
  <p class="row mut edit" id="speedrow"><span>Uploads slow?</span><button id="speedbtn">Test connection speed</button><span id="speedout" aria-live="polite"></span></p>
</div></section>

<section id="p-notes" hidden>
  <div class="card">
    <div class="row"><span>Posting as <strong id="myname"></strong></span><button id="rename">Change name</button></div>
    <div id="newpost" class="edit"></div>
  </div>
  <div id="threads"></div>
  <p class="mut" id="nonotes" hidden>No notes yet. Start a thread above: share a link, ask a question, or attach some files.</p>
</section>

<section id="p-settings" hidden><div class="grid2">
  <div class="card"><h2>Your link</h2>
    <p class="row"><span id="slink"></span><button id="scopy">Copy</button></p>
    <form id="linkf">
      <p class="row"><label for="lname">http://</label><input id="lname" maxlength="40" size="14" required aria-label="Link name">
        <label for="lcode">.local/</label><input id="lcode" maxlength="40" size="12" placeholder="optional code" aria-label="Code at the end of the link (optional)"></p>
      <p class="row"><button class="pri">Save link</button><button type="button" id="newkey">Make a secure link</button><button type="button" id="nocode">Remove the code</button></p>
    </form>
    <p class="mut" id="linkhint"></p>
  </div>
  <div class="card"><h2>Sharing</h2>
    <p><label><input type="checkbox" id="lan"> Let other devices on my network connect</label></p>
    <p><label><input type="checkbox" id="gedit"> Other devices can add, rename, move and delete files, and post notes</label><br><span class="mut">Turn this off to let them only view and download.</span></p>
    <p class="row"><label for="stoph">Stop sharing automatically after:</label>
      <select id="stoph"><option value="0">Never</option><option value="1">1 hour</option><option value="2">2 hours</option><option value="4">4 hours</option><option value="8">8 hours</option></select>
      <span class="mut" id="stopat"></span></p>
    <p id="autobox"><label><input type="checkbox" id="auto"> Start HomeShare when this computer starts</label></p>
    <form id="hnamef" class="row"><label for="hname">This computer's name in notes:</label><input id="hname" maxlength="40" required><button>Save</button></form>
    <p class="row"><span id="folder"></span><button id="openf">Open folder</button><button id="chf">Change folder…</button></p>
    <p class="row"><span id="trashn"></span><button id="trashv">View trash</button><button id="trashe">Empty trash</button></p>
  </div>
  <div class="card"><h2>Security</h2>
    <p id="fw"></p>
    <div id="nets"></div>
    <p><label><input type="checkbox" id="appr"> Ask me before a new device can connect</label></p>
    <form id="pinsf" class="row"><label for="pinset" id="pinlbl">PIN for other devices:</label><input id="pinset" type="password" maxlength="64" autocomplete="new-password"><button id="pinbtn">Set PIN</button><button type="button" id="pinrm">Remove PIN</button></form>
    <p><label><input type="checkbox" id="qrpin"> Scanning the QR code skips the PIN</label></p>
  </div>
  <div class="card"><h2>Turn off</h2><p class="row"><button class="danger" id="stop">Close HomeShare</button><span class="mut">Stops sharing and closes the program. Open it again any time.</span></p></div>
</div></section>
</main>
<div id="drop" aria-hidden="true" hidden>Drop files or folders to share them</div>
<div id="viewer" role="dialog" aria-modal="true" aria-label="Preview" hidden>
  <div class="vbar"><span id="vname"></span><button id="vframe" hidden>Use this frame as thumbnail</button><a class="btn" id="vdl">Download</a><button id="vclose" aria-label="Close preview">✕ Close</button></div>
  <div id="vbody"></div>
  <button class="vnav" id="vprev" aria-label="Previous">‹</button><button class="vnav" id="vnext" aria-label="Next">›</button>
</div>
<dialog id="thumbdlg" aria-labelledby="tdtitle">
  <h2 id="tdtitle"></h2>
  <p class="mut">Choose the picture that shows for this file, on every device.</p>
  <div class="row"><button class="pri" id="tdpick">Choose a picture…</button><button id="tdframe">Pick a frame from the video</button>
    <button id="tdreset">Go back to automatic</button><button id="tdclose">Cancel</button></div>
  <input type="file" id="tdfile" accept="image/*" hidden>
</dialog>
<script nonce="__NONCE__">
const $ = id => document.getElementById(id);
const TRASH = ".trash", TABS = ["home", "files", "notes", "settings"];
const EXT = {image: "jpg jpeg png gif webp bmp svg avif ico", video: "mp4 webm mov m4v ogv", audio: "mp3 m4a aac wav ogg oga flac opus",
             pdf: "pdf", text: "txt md csv json log xml ini cfg yml yaml css js py html htm srt",
             doc: "doc docx docm xls xlsx xlsm ppt pptx pptm odt ods odp odg rtf"};
const ICON = {folder: "📁", image: "🖼️", video: "🎬", audio: "🎵", pdf: "📕", text: "📝", doc: "📃", file: "📄"};
const BADGE = {image: "📷", video: "🎬", audio: "🎵", pdf: "📄", text: "📄", doc: "📄"};  // grid view: what kind of file this is
const VIEW = ["image", "video", "audio", "pdf", "text"];  // kinds that open in the viewer; the rest download
let I = {}, routed = false, tab = "", dir = null, items = [], shown = [], lastSig = "", sortKey = "n", asc = true, sel = new Set(),
    dragging = null, depth = 0, stopped = false, view = "list", vlist = [], vi = 0, jobs = [], active = 0,
    notesSig = "", actLimit = 15, qrSrc = "";
const drafts = {};  // reply boxes survive refreshes, so half-typed replies aren't lost
const touch = matchMedia("(pointer: coarse)").matches;
try { view = localStorage.getItem("hs-view") || "list"; } catch {}

const join = (a, b) => a ? a + "/" + b : b;
const enc = p => p.split("/").map(encodeURIComponent).join("/");
const fileUrl = p => "/f/" + enc(p);
const el = (tag, props = {}, ...kids) => { const e = Object.assign(document.createElement(tag), props); e.append(...kids); return e; };
const btn = (label, onclick, className = "") => el("button", {textContent: label, onclick, className});
const sleep = ms => new Promise(r => setTimeout(r, ms));
const esc = s => s.replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
const kindOf = n => { const e = n.includes(".") ? n.split(".").pop().toLowerCase() : ""; for (const k in EXT) if (EXT[k].split(" ").includes(e)) return k; return "file"; };
const msg = t => $("msg").textContent = t;
function fmtSize(b) { const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (b >= 1024 && i < 4) { b /= 1024; i++; } return (i ? b.toFixed(1) : b) + " " + u[i]; }
const fmtDate = s => new Date(s * 1000).toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
const fmtClock = s => new Date(s * 1000).toLocaleTimeString([], {timeStyle: "short"});
const fmtTime = s => s < 60 ? Math.ceil(s) + " s" : s < 3600 ? Math.round(s / 60) + " min" : (s / 3600).toFixed(1) + " h";
function ago(t) { const s = Date.now() / 1000 - t; return s < 60 ? "just now" : s < 3600 ? Math.floor(s / 60) + " min ago" : s < 86400 ? Math.floor(s / 3600) + " h ago" : fmtDate(t); }

async function api(url, body, wait = 20000) {  // never wait forever: a phone can lose a connection without being told
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), wait);
  let r;
  try { r = await fetch(url, {signal: ctl.signal, ...(body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {})}); }
  catch { throw Object.assign(new Error("Can't reach the sharing computer right now. Check you're on the home Wi-Fi."), {offline: true}); }
  finally { clearTimeout(timer); }
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && j.need) { I.need = j.need; route(); throw new Error(j.error); }
  if (!r.ok) throw new Error(j.error || "Something went wrong.");
  return j;
}
const act = f => async (...a) => { try { await f(...a); } catch (e) { alert(e.message); } lastSig = ""; notesSig = ""; refresh(true); };

async function copyText(t, b) {
  try { await navigator.clipboard.writeText(t); }
  catch { const a = el("textarea", {value: t}); document.body.append(a); a.select(); document.execCommand("copy"); a.remove(); }
  b.dataset.label ||= b.textContent;
  b.textContent = "Copied!"; setTimeout(() => b.textContent = b.dataset.label, 1500);
}

// ---- tabs ----
async function info() {
  const before = I.need;
  I = await api("/api/info");
  document.body.classList.toggle("guest", !I.host);
  document.body.classList.toggle("ro", !I.edit);
  $("sub").textContent = I.host ? "" : "· shared from " + I.name;
  let seen = 0; try { seen = +localStorage.getItem("hs-notes-seen") || 0; } catch {}
  $("notesdot").hidden = tab === "notes" || !(I.notes_last > seen);
  $("homedot").hidden = !(I.host && I.pending);
  $("speedrow").hidden = I.host;  // the sharing computer talking to itself would say nothing about the Wi-Fi
  if (routed && I.need !== before) route();
}
function route() {
  routed = true;
  const parts = location.hash.slice(1).split("/");
  let t = TABS.includes(parts[0]) ? parts[0] : I.host ? "home" : "files";
  if (!I.host && (t === "home" || t === "settings")) t = "files";
  if (I.need) t = "gate";
  tab = t;
  for (const p of ["gate", ...TABS]) $("p-" + p).hidden = p !== t;
  document.querySelectorAll("#tabs a").forEach(a => a.dataset.t === t ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"));
  $("tabs").hidden = t === "gate";
  closeViewer();
  document.body.classList.remove("intrash");
  if (t === "gate") return gate();
  if (t === "files") {
    let d = ""; try { d = parts.slice(1).map(decodeURIComponent).join("/"); } catch {}
    if (d !== dir) { dir = d; sel.clear(); lastSig = ""; $("search").value = ""; }
    document.body.classList.toggle("intrash", dir === TRASH);
    load();
  }
  if (t === "notes") { notesSig = ""; loadNotes(); }
  if (t === "home") loadHome();
  if (t === "settings") settings();
}
onhashchange = route;
// ---- when HomeShare goes away (closed, ended in Task Manager, laptop off): say so, and come back by itself ----
let misses = 0, sawDown = false;
function showEnded() {
  if (stopped) return;
  stopped = true; sawDown = false;
  if (live) { live.close(); live = null; }
  if (!retired) waitForReturn();
  closeViewer(); $("tabs").hidden = true; $("jobs").hidden = true;
  document.querySelector("main").replaceChildren(el("section", {className: "card"}, ...(I.host
    ? [el("h2", {textContent: "HomeShare is closed"}), el("p", {textContent: "Nothing is being shared. You can close this tab, and open HomeShare again whenever you want to share."})]
    : [el("h2", {textContent: "The host has ended their HomeShare session"}),
       el("p", {textContent: "Files and notes aren't available right now. This page reconnects by itself if they start sharing again."})])));
}
// While closed, keep one connection attempt going (the browser retries every few seconds, even in a background tab).
// It gets through as soon as HomeShare is back, which also tells HomeShare this tab exists so it won't open another.
function waitForReturn() {
  const es = new EventSource("/api/live");
  es.onerror = () => { sawDown = true; };
  es.onopen = () => {
    if (!sawDown || retired) return;  // still shutting down, or another tab took over
    es.close();
    try { sessionStorage.setItem("hs-auto", "1"); } catch {}
    location.reload();
  };
}
let live = null;
function watchLive() {  // the held-open connection: if it drops, check quickly whether HomeShare has really gone
  if (live) live.close();
  live = new EventSource("/api/live");
  live.onerror = async () => {
    live.close(); live = null;
    if (stopped) return;
    try { await api("/api/info", null, 2500); setTimeout(watchLive, 1000); }  // just a blip: hold on again
    catch (e) { if (e.offline) showEnded(); else setTimeout(watchLive, 1000); }
  };
}
async function refresh(force) {
  if (retired) return;  // another tab is the HomeShare tab now: this one stays quiet for good
  if (stopped) {  // keep checking quietly; once it's been seen gone and is back, pick up where things were
    try {
      await api("/api/info", null, 5000);
      if (sawDown) { try { sessionStorage.setItem("hs-auto", "1"); } catch {} location.reload(); }  // automatic: doesn't claim
    } catch { sawDown = true; }
    return;
  }
  try { await info(); misses = 0; } catch (e) { if (e.offline && ++misses >= 2) showEnded(); return; }  // 2 in a row: not just a Wi-Fi blip
  if (I.need) return;
  if (tab === "files" && (force || (!dragging && $("viewer").hidden))) load();  // keeps going during uploads: new files show up everywhere
  if (tab === "notes") loadNotes();
  if (tab === "home") loadHome();
  if (tab === "settings" && force) settings();
}

// ---- what a device sees before it's let in ----
function gate() {
  const n = I.need, g = $("gate");
  const [title, text] = {link: ["🔗 You need the full link", "Ask the person sharing for the link, or scan the QR code on their screen."],
    pin: ["🔒 This share has a PIN", "Type the PIN the person sharing gave you."],
    approval: ["⏳ Waiting to be let in", "The sharing computer needs to allow this device. This page opens by itself once they do."],
    blocked: ["🚫 This device was removed", "The person sharing removed this device. Ask them if you need to get back in."],
    paused: ["⏸️ Sharing is paused", "The sharing computer isn't on its home network right now."]}[n];
  g.replaceChildren(el("h2", {textContent: title}), el("p", {textContent: text}));
  if (n === "pin") {
    const inp = el("input", {type: "password", id: "pin", autocomplete: "off"}), out = el("p", {className: "mut"});
    const f = el("form", {className: "row"}, el("label", {htmlFor: "pin", textContent: "PIN:"}), inp, el("button", {className: "pri", textContent: "Unlock"}));
    f.onsubmit = async e => { e.preventDefault(); try { await api("/api/login", {pin: inp.value}); await info(); route(); } catch (err) { out.textContent = err.message; } };
    g.append(f, out); inp.focus();
  }
  if (n === "approval") {
    const inp = el("input", {id: "who", maxLength: 40, value: I.me.name, placeholder: "e.g. Mom's iPad"}), out = el("p", {className: "mut"});
    const f = el("form", {className: "row"}, el("label", {htmlFor: "who", textContent: "Tell them who you are:"}), inp, el("button", {textContent: "Send"}));
    f.onsubmit = async e => { e.preventDefault(); try { await api("/api/me", {name: inp.value}); out.textContent = "Sent. They'll see “" + inp.value.trim() + "”."; } catch (err) { out.textContent = err.message; } };
    g.append(f, out);
  }
}

// ---- Home (sharing computer only) ----
const fixFirewall = act(async () => { $("status").textContent = "Click “Yes” on the Windows prompt (it may be flashing in the taskbar)…"; await api("/api/firewall", {}, 120000); });
const trust = name => act(() => api("/api/trust", {name, on: true}));
function statusLine() {
  if (!I.lan) return [el("span", {className: "warn", textContent: "⏸️ Sharing is off. Only this computer can see your files. "}),
    btn("Turn sharing on", () => setting("lan", true), "pri")];
  const off = btn("Turn sharing off", () => setting("lan", false));
  if (!I.net_ok) return [el("span", {className: "warn", textContent: "⏸️ Paused: this computer is on “" + I.network + "”, which isn't one of your home networks. "}), btn("Share on this network too", trust(I.network)), " ", off];
  if (!I.firewall) return [el("span", {className: "warn", textContent: "⚠️ Windows Firewall is blocking other devices. "}), btn("Fix this", fixFirewall), " ", off];
  const s = ["✅ Sharing is on."];
  if (I.online) s.push(I.online + (I.online === 1 ? " device" : " devices") + " connected.");
  if (I.pending) s.push(I.pending + " waiting for you to let them in (see Devices).");
  if (I.stop_at) s.push("Stops at " + fmtClock(I.stop_at) + ".");
  return [el("span", {className: "ok", textContent: s.join(" ") + " "}), off];
}
const devAct = (d, action) => act(async () => {
  if (action === "block" && !confirm("Remove " + (d.name || d.label) + "? It won't be able to open HomeShare any more.\nTo be extra sure, also make a new link in Settings.")) return;
  await api("/api/device", {id: d.id, action});
});
function devRow(d) {
  const st = d.status === "pending" ? "Waiting for you" : d.status === "blocked" ? "Removed" : d.online ? "Online" : "Seen " + ago(d.seen);
  const acts = d.status === "pending" ? [btn("Let in", devAct(d, "allow"), "pri"), btn("Deny", devAct(d, "block"))]
    : d.status === "blocked" ? [btn("Let back in", devAct(d, "allow")), btn("Forget", devAct(d, "forget"))] : [btn("Remove", devAct(d, "block"))];
  return el("div", {className: "dev"}, el("div", {className: "who"}, el("strong", {textContent: d.name || d.label}), d.name ? el("div", {className: "mut", textContent: d.label}) : ""),
    el("span", {className: "pill" + (d.status === "pending" ? " warn" : d.online ? " ok" : ""), textContent: st}), ...acts);
}
async function loadHome() {
  if (!I.host) return;
  $("status").replaceChildren(...statusLine());
  $("link").textContent = I.links[0];
  $("alt").textContent = "If that doesn't open on a device, use " + I.links[1] + " instead.";
  $("qrbox").hidden = !I.qr;
  const q = "/api/qr.svg?v=" + encodeURIComponent(I.links[1] + I.pin_set + I.qr_pin);
  if (I.qr && q !== qrSrc) $("qr").src = qrSrc = q;
  $("hfolder").textContent = "Sharing the folder: " + I.folder;
  $("hdisk").textContent = fmtSize(I.free) + " free on this drive.";
  $("hver").textContent = "HomeShare version " + I.version;
  const [dv, ac] = await Promise.all([api("/api/devices"), api("/api/activity")]);
  $("devs").replaceChildren(...(dv.devices.length ? dv.devices.map(devRow) : [el("p", {className: "mut", textContent: "No other devices yet. Share the link or QR code to get started."})]));
  $("acts").replaceChildren(...(ac.items.length
    ? [el("div", {className: "act acthead mut"}, el("span", {textContent: "When"}), el("span", {textContent: "What"}),
         el("span", {className: "acol", textContent: "Type"}), el("span", {className: "acol", textContent: "Size"}), el("span")),
       ...ac.items.slice(0, actLimit).map(actRow)]
    : [el("p", {className: "mut", textContent: "Nothing yet. Uploads, downloads, deletions and new devices show up here."})]));
  $("actmore").hidden = ac.items.length <= actLimit;
  $("actclear").hidden = !ac.items.length;
}
const KIND_LABEL = {folder: "Folder", image: "Photo", video: "Video", audio: "Music", pdf: "PDF", text: "Text", doc: "Document", file: "File"};
function actRow(a) {
  const name = a.path ? a.path.split("/").pop() : "", kind = a.path ? (a.dir ? "folder" : kindOf(name)) : "";
  const what = el("span", {className: "awhat"}, el("strong", {textContent: a.who}), " " + a.what);
  if (a.removed) what.append(" ", el("span", {className: "pill", textContent: a.in_trash ? "🗑️ Removed" : "🗑️ Gone",
    title: a.in_trash ? "This file was removed. Reclaim puts it back." : "This file was removed and the trash has since been emptied."}));
  let action = el("span");  // remove the file (into the trash, no questions), or reclaim it (asks first)
  if (a.removed && a.in_trash) action = btn("Reclaim", act(async () => {
    if (confirm("Put “" + name + "” back where it was?")) await api("/api/activity/reclaim", {id: a.id});
  }));
  else if (a.path && !a.removed && a.exists) action = btn("Remove file", act(() => api("/api/activity/remove", {id: a.id})));
  return el("div", {className: "act"}, el("span", {className: "mut", textContent: ago(a.t), title: fmtDate(a.t)}), what,
    el("span", {className: "mut acol", textContent: KIND_LABEL[kind] || ""}),
    el("span", {className: "mut acol", textContent: a.size != null ? fmtSize(a.size) : ""}), action);
}
$("actmore").onclick = () => { actLimit += 50; loadHome(); };
$("actclear").onclick = act(async () => { if (confirm("Clear the recent activity list? This can't be undone. (Files aren't touched.)")) await api("/api/activity/clear", {}); });
$("copy").onclick = e => copyText($("link").textContent, e.target);
$("openf2").onclick = act(() => api("/api/open", {}));

// ---- Settings (sharing computer only) ----
function settings() {
  if (!I.host) return;
  $("lan").checked = I.lan; $("gedit").checked = I.guest_edit; $("stoph").value = String(I.stop_hours);
  $("stopat").textContent = I.stop_at ? "Stops at " + fmtClock(I.stop_at) + "." : "";
  $("autobox").hidden = I.autostart === null; $("auto").checked = !!I.autostart;
  $("lname").value = I.link_name; $("lcode").value = I.link_code; $("hname").value = I.me.name;
  $("slink").textContent = I.links[0]; $("nocode").hidden = !I.link_code;
  $("linkhint").textContent = (!I.link_code ? "Anyone on your Wi-Fi who opens this link can get in. Add a code, or click “Make a secure link”, to keep it to people you give the link to."
    : I.link_code.length < 8 ? "Short codes are easy to guess. “Make a secure link” picks a long random code instead."
    : "Only people with this exact link or the QR code can get in.") + " Changing the link means everyone needs the new one.";
  $("folder").textContent = "Sharing the folder: " + I.folder;
  $("trashn").textContent = "Trash: " + I.trash + (I.trash === 1 ? " item" : " items");
  $("fw").replaceChildren(...(I.firewall ? [el("span", {className: "ok", textContent: "✅ Windows Firewall lets devices on your home network connect."})]
    : [el("span", {className: "warn", textContent: "⚠️ Windows Firewall is blocking other devices. "}), btn("Fix this", fixFirewall)]));
  $("nets").replaceChildren(
    el("p", {}, "Home networks (sharing pauses on any other Wi-Fi): ", ...(I.trusted.length ? I.trusted.map(n => el("span", {className: "chip"}, n,
      Object.assign(btn("✕", act(() => api("/api/trust", {name: n, on: false}))), {ariaLabel: "Remove " + n}))) : [el("span", {className: "mut", textContent: "none yet"})])),
    el("p", {className: "row"}, el("span", {className: "mut", textContent: I.network ? "This computer is on “" + I.network + "”" + (I.net_ok ? ", a home network." : ", which isn't a home network.") : ""}),
      I.network && !I.net_ok ? btn("Share on this network too", trust(I.network)) : ""));
  $("appr").checked = I.approve; $("qrpin").checked = I.qr_pin;
  $("pinlbl").textContent = I.pin_set ? "A PIN is set. New PIN:" : "PIN for other devices:";
  $("pinbtn").textContent = I.pin_set ? "Change PIN" : "Set PIN"; $("pinrm").hidden = !I.pin_set; $("pinset").value = "";
}
const setting = (k, v) => act(() => api("/api/settings", {[k]: v}))();
$("lan").onchange = () => setting("lan", $("lan").checked);
$("gedit").onchange = () => setting("guest_edit", $("gedit").checked);
$("stoph").onchange = () => setting("stop_hours", +$("stoph").value);
$("auto").onchange = () => setting("autostart", $("auto").checked);
$("appr").onchange = () => setting("approve", $("appr").checked);
$("qrpin").onchange = () => setting("qr_pin", $("qrpin").checked);
$("pinsf").onsubmit = e => { e.preventDefault(); if ($("pinset").value.trim()) setting("pin", $("pinset").value); };
$("pinrm").onclick = () => { if (confirm("Remove the PIN?")) setting("pin", ""); };
$("newkey").onclick = act(async () => { if (confirm("Make a secure link with a long random code? The current link and QR code stop working, so everyone needs the new one.")) await api("/api/newkey", {}); });
$("nocode").onclick = act(async () => { if (confirm("Remove the code? Anyone on your Wi-Fi will be able to open HomeShare.")) await api("/api/link", {code: ""}); });
$("linkf").onsubmit = e => { e.preventDefault(); act(() => api("/api/link", {name: $("lname").value, code: $("lcode").value}))(); };
$("scopy").onclick = e => copyText(I.links[0], e.target);
$("hnamef").onsubmit = e => { e.preventDefault(); act(() => api("/api/me", {name: $("hname").value}))(); };
$("openf").onclick = act(() => api("/api/open", {}));
$("chf").onclick = $("chf2").onclick = act(async () => { await api("/api/folder", {}, 600000); dir = null; });  // waits while they pick
$("trashv").onclick = () => location.replace("#files/" + TRASH);
$("trashe").onclick = act(async () => { if (confirm("Permanently delete everything in the trash? This can't be undone.")) await api("/api/emptytrash", {}); });
$("stop").onclick = $("close2").onclick = async () => {
  if (!confirm("Close HomeShare? Other devices lose access until you open it again.")) return;
  await api("/api/stop", {}).catch(() => {});
  showEnded();
};

// ---- Files ----
async function load() {
  if (stopped || I.need || dir === null) return;
  try {
    const j = await api("/api/list?dir=" + encodeURIComponent(dir));
    const sig = JSON.stringify(j.items);
    if (sig === lastSig) return;  // nothing changed: don't rebuild (keeps videos and scroll steady)
    lastSig = sig; items = j.items;
    for (const n of [...sel]) if (!items.some(i => i.n === n)) sel.delete(n);
    render();
  } catch (e) { msg(e.message); if (dir && !I.need) location.replace("#files"); }
}
function render() {
  const parts = dir ? dir.split("/") : [];
  $("crumbs").replaceChildren();
  [["Home folder", ""], ...parts.map((p, i) => [p === TRASH ? "Trash" : p, parts.slice(0, i + 1).join("/")])].forEach(([label, path], i) => {
    if (i) $("crumbs").append(" / ");
    const a = el("a", {href: "#files/" + enc(path), textContent: label});
    dropTarget(a, path);
    $("crumbs").append(a);
  });
  const q = $("search").value.trim().toLowerCase();
  const cmp = (a, b) => sortKey === "n" ? a.n.localeCompare(b.n, undefined, {numeric: true, sensitivity: "base"}) : a[sortKey] - b[sortKey];
  shown = items.filter(i => !q || i.n.toLowerCase().includes(q)).sort((a, b) => (b.d - a.d) || cmp(a, b) * (asc ? 1 : -1));
  $("list").className = view;
  $("list").replaceChildren(...shown.map(item));
  $("empty").hidden = shown.length > 0;
  $("empty").textContent = q ? "Nothing here matches your search." : dir === TRASH ? "The trash is empty."
    : I.edit ? "This folder is empty. Drag files or folders onto this page, or click Upload files." : "This folder is empty.";
  $("viewt").textContent = view === "grid" ? "List view" : "Grid view";
  $("sortd").textContent = asc ? "↑" : "↓";
  renderSel();
}
const io = new IntersectionObserver(es => es.forEach(e => {
  if (e.isIntersecting) { e.target.preload = "metadata"; e.target.src = e.target.dataset.src; io.unobserve(e.target); }
}), {rootMargin: "300px"});
function thumb(it, path, kind) {
  const icon = () => el("span", {className: "ic", textContent: ICON[kind]});
  if (it.d || (kind === "file" && !it.t)) return icon();
  if (kind === "video" && !it.t) {  // a frame from the video itself, loaded once it scrolls into view (both views)
    const v = el("video", {muted: true, preload: "none", playsInline: true});
    v.dataset.src = fileUrl(path) + "#t=1"; v.onerror = () => v.replaceWith(icon()); io.observe(v);
    return v;
  }
  // photos, album art, document pages, or a picture someone chose; the computer answers "no preview" if there's none
  const i = el("img", {src: "/thumb/" + enc(path) + "?v=" + it.m + "." + (it.t || 0), loading: "lazy", alt: ""});
  i.onerror = () => i.replaceWith(icon());
  return i;
}
function viewables(list, base) { return list.filter(i => !i.d && VIEW.includes(kindOf(i.n))).map(i => ({path: join(base, i.n), n: i.n, s: i.s})); }
function item(it) {
  const path = join(dir, it.n), kind = it.d ? "folder" : kindOf(it.n), inTrash = dir === TRASH;
  const box = el("input", {type: "checkbox", checked: sel.has(it.n), ariaLabel: "Select " + it.n});
  box.onchange = () => { box.checked ? sel.add(it.n) : sel.delete(it.n); box.closest(".item").classList.toggle("on", box.checked); renderSel(); };
  const viewable = VIEW.includes(kind);
  const name = el("a", {className: "nm", textContent: it.n, title: it.n, href: it.d ? "#files/" + enc(path) : fileUrl(path) + (viewable ? "" : "?dl=1")});
  if (!it.d && !viewable) name.download = it.n;
  else if (!it.d) name.onclick = e => { e.preventDefault(); const l = viewables(shown, dir); openViewer(l, l.findIndex(x => x.n === it.n)); };
  const pic = el("div", {className: "pic", ariaHidden: "true"}, thumb(it, path, kind), ...(BADGE[kind] ? [el("span", {className: "badge", textContent: BADGE[kind]})] : []));
  pic.onclick = () => name.click();
  const acts = el("span", {className: "rowacts"});
  if (!it.d) {
    if (I.edit && !inTrash) acts.append(Object.assign(btn("🖼️", () => thumbMenu(it, path, kind), "thb"), {title: "Change thumbnail", ariaLabel: "Change the thumbnail of " + it.n}));
    acts.append(el("a", {className: "btn dl", href: fileUrl(path) + "?dl=1", download: it.n, textContent: "Download", ariaLabel: "Download " + it.n}));
  }
  // drag-to-move is for mouse users; on touch screens a long press could start a drag that never ends and freeze the list
  const div = el("div", {className: "item" + (sel.has(it.n) ? " on" : ""), draggable: I.edit && !inTrash && !touch}, box, pic, name,
    el("span", {className: "sz mut", textContent: it.d ? "" : fmtSize(it.s)}),
    el("span", {className: "dt mut", textContent: fmtDate(it.m)}), acts);
  div.ondragstart = e => { dragging = sel.has(it.n) ? [...sel] : [it.n]; e.dataTransfer.setData("text/plain", it.n); };
  div.ondragend = () => dragging = null;
  if (it.d && !inTrash) dropTarget(div, path);
  return div;
}
// drag items onto a folder or a breadcrumb to move them there
function dropTarget(node, target) {
  node.ondragover = e => { if (dragging && target !== dir && !dragging.some(n => join(dir, n) === target)) { e.preventDefault(); node.classList.add("drop"); } };
  node.ondragleave = () => node.classList.remove("drop");
  node.ondrop = e => {
    node.classList.remove("drop");
    if (!dragging) return;
    e.preventDefault(); e.stopPropagation();
    const names = dragging; dragging = null;
    act(() => moveNames(names, target))();
  };
}
async function moveNames(names, to) { for (const n of names) await api("/api/move", {path: join(dir, n), to: join(to, n)}); sel.clear(); }
function renderSel() {
  $("selbar").hidden = !sel.size;
  $("selcount").textContent = sel.size + " selected";
  $("selren").hidden = sel.size !== 1;
  const one = sel.size === 1 && items.find(i => i.n === [...sel][0]);
  $("selthumb").hidden = !one || one.d;
  $("selall").checked = sel.size > 0 && sel.size === shown.length;
}
$("selall").onchange = () => { sel.clear(); if ($("selall").checked) shown.forEach(i => sel.add(i.n)); render(); };
$("selclr").onclick = () => { sel.clear(); render(); };
$("seldl").onclick = () => download([...sel]);
$("selren").onclick = act(async () => {
  const old = [...sel][0], n = prompt("New name:", old);
  if (n && n !== old) { await api("/api/move", {path: join(dir, old), to: join(dir, n)}); sel.clear(); }
});
$("selmv").onclick = act(async () => {
  const to = prompt("Move " + sel.size + (sel.size === 1 ? " item" : " items") + " to which folder?\nType a folder path like Photos/2024, or leave it empty for the home folder.", dir);
  if (to !== null) await moveNames([...sel], to.replace(/^\/+|\/+$/g, ""));
});
$("seldel").onclick = act(async () => {
  if (!confirm("Delete " + sel.size + (sel.size === 1 ? " item" : " items") + "?\nThey move to the trash on the sharing computer.")) return;
  for (const n of [...sel]) await api("/api/delete", {path: join(dir, n)});
  sel.clear();
});
$("selres").onclick = act(async () => { for (const n of [...sel]) await api("/api/restore", {name: n}); sel.clear(); });
function download(names) {
  const one = names.length === 1 && items.find(i => i.n === names[0]);
  if (one && !one.d) { location.href = fileUrl(join(dir, one.n)) + "?dl=1"; return; }
  const f = el("form", {method: "POST", action: "/zip"}, el("input", {type: "hidden", name: "dir", value: dir}),
    ...names.map(n => el("input", {type: "hidden", name: "n", value: n})));
  document.body.append(f); f.submit(); f.remove();
}
$("search").oninput = render;
$("sortk").onchange = () => { sortKey = $("sortk").value; render(); };
$("sortd").onclick = () => { asc = !asc; render(); };
$("viewt").onclick = () => { view = view === "grid" ? "list" : "grid"; try { localStorage.setItem("hs-view", view); } catch {} render(); };
$("mk").onclick = act(async () => { const n = prompt("Name for the new folder:"); if (n) await api("/api/mkdir", {path: join(dir, n)}); });
$("up").onclick = () => $("pick").click();
$("upf").onclick = () => $("pickd").click();
$("pick").onchange = () => { queue([...$("pick").files].map(f => ({file: f, rel: f.name}))); $("pick").value = ""; };
$("pickd").onchange = () => { queue([...$("pickd").files].map(f => ({file: f, rel: f.webkitRelativePath || f.name}))); $("pickd").value = ""; };

// ---- speed test: throwaway data both ways, no files, so it shows the Wi-Fi itself ----
const sendTest = mb => new Promise((ok, no) => {  // finishes only when the computer confirms it has every byte
  const x = new XMLHttpRequest();
  x.open("PUT", "/api/speedtest");
  x.onload = () => x.status < 300 ? ok() : no(new Error("The test didn't work (" + x.status + ")."));
  x.onerror = () => no(new Error("Connection lost during the test."));
  x.send(new Blob([new Uint8Array(mb << 20)]));
});
async function timed(mb, run) { const t = performance.now(); await run(); return mb / ((performance.now() - t) / 1000); }
$("speedbtn").onclick = async () => {
  const out = $("speedout"), b = $("speedbtn");
  b.disabled = true;
  try {
    out.textContent = "Testing upload over 1 connection…";
    const one = await timed(24, () => sendTest(24));
    out.textContent = "Testing upload over 4 connections at once…";
    const four = await timed(48, () => Promise.all([12, 12, 12, 12].map(sendTest)));
    out.textContent = "Testing download…";
    const down = await timed(32, async () => (await fetch("/api/speedtest?mb=32", {cache: "no-store"})).arrayBuffer());
    out.textContent = "Upload to the sharing computer: " + one.toFixed(1) + " MB/s over 1 connection, " + four.toFixed(1) +
      " MB/s over 4 at once · download: " + down.toFixed(1) + " MB/s. " + (four > one * 1.6
      ? "Several connections are much faster here, so HomeShare could speed up big files by splitting them."
      : one < 10 ? "More connections don't help, so the Wi-Fi path itself is the limit. A network cable on the sharing computer would help most."
      : "The connection is fine.");
  } catch (e) { out.textContent = e.message; }
  b.disabled = false;
};

// ---- uploads: each file in one request, a few at a time ----
function queue(entries, base = dir, onChange = null) {  // onChange: called whenever one of these uploads moves on
  const added = entries.map(({file, rel}) => ({file, path: join(base, rel), state: "waiting", sent: 0, onChange}));
  jobs.push(...added);
  renderJobs(); pump();
  return added;
}
function status(j) {
  if (j.state === "waiting") return "Waiting";
  if (j.state === "retrying") return (j.why || "Connection lost") + ", trying again…";
  if (j.state === "failed") return j.err;
  // speed over the last few seconds, not since the start: a phone first reports a burst it has only queued, which made
  // the running average start high and sink
  const h = j.hist || [], [t0, s0] = h[0] || [0, 0], secs = h.length ? (Date.now() - t0) / 1000 : 0;
  const speed = secs > 1 ? (j.sent - s0) / secs : 0;
  const pct = Math.floor(100 * j.sent / (j.file.size || 1));
  return pct + "%" + (speed > 0 ? " · " + fmtSize(speed) + "/s · " + fmtTime((j.file.size - j.sent) / speed) + " left" : "");
}
function tick(j) {
  const now = Date.now();
  (j.hist ||= []).push([now, j.sent]);
  while (j.hist.length > 2 && now - j.hist[1][0] > 5000) j.hist.shift();  // keep about the last 5 seconds
  if (j.bar) { j.bar.value = j.sent; j.txt.textContent = status(j); }
  if (j.onChange) j.onChange();
}
function renderJobs() {
  const live = jobs.filter(j => j.state !== "done" && j.state !== "cancelled");
  for (const j of jobs) if (j.onChange) j.onChange();
  $("jobs").hidden = !live.length;
  if (!live.length) return;
  const failed = live.filter(j => j.state === "failed").length;
  $("jobsum").textContent = failed === live.length ? (failed === 1 ? "1 upload didn't finish" : failed + " uploads didn't finish")
    : "Uploading " + (jobs.length === 1 ? "1 file" : jobs.length + " files · " + (jobs.length - live.length) + " done");
  $("joblist").replaceChildren(...live.slice(0, 50).map(j => {
    j.bar = el("progress", {max: j.file.size || 1, value: j.sent});
    j.txt = el("span", {className: j.state === "failed" ? "warn" : "mut", textContent: status(j)});
    const acts = j.state === "failed" ? [btn("Retry", () => retry(j)), " ", btn("Dismiss", () => cancel(j))] : [btn("Cancel", () => cancel(j))];
    return el("div", {className: "job"}, el("span", {textContent: j.path, title: j.path}), j.bar, el("span", {}, j.txt, " ", ...acts));
  }));
}
function cancel(j) {
  const running = j.state !== "failed";
  j.state = "cancelled";
  if (running && j.xhr) j.xhr.abort();
  renderJobs();
}
function retry(j) { if (j.state === "failed") { j.state = "waiting"; j.err = ""; renderJobs(); pump(); } }
$("jobcancel").onclick = () => jobs.filter(j => j.state !== "done" && j.state !== "cancelled").forEach(cancel);
addEventListener("online", () => jobs.filter(j => j.state === "failed" && !j.fatal).forEach(retry));  // back on Wi-Fi: carry on by itself
function pump() {  // up to 3 files at once: one connection rarely uses all of a phone's Wi-Fi
  let job;
  while (active < 3 && (job = jobs.find(j => j.state === "waiting"))) {
    active++;
    job.state = "uploading";
    upload(job).finally(() => { active--; renderJobs(); if (tab === "files") load(); pump(); });
  }
  if (active || jobs.some(j => j.state === "waiting")) return;
  renderJobs();
  if (!jobs.some(j => j.state === "failed")) {
    const n = jobs.filter(j => j.state === "done").length;
    if (n && tab === "files") msg(n + (n === 1 ? " file" : " files") + " uploaded.");
    jobs = [];
  }
  if (retireLater) retire();  // another tab took over while this one was uploading
}
async function upload(job) {
  const waits = [3000, 10000, 30000];  // a dropped connection starts the file again by itself over ~45 s, then offers Retry
  for (let attempt = 0; ; attempt++) {
    try { job.final = (await send(job)).path; job.state = "done"; break; }
    catch (e) {
      if (job.state === "cancelled") break;
      if (e.fatal || attempt >= waits.length) { job.state = "failed"; job.err = e.message; job.fatal = !!e.fatal; break; }
      job.state = "retrying"; job.why = e.message; tick(job);
      await sleep(waits[attempt]);
      if (job.state === "cancelled") break;
    }
  }
}
function send(job) {  // the whole file in one request, as in the first version that worked on every device
  return new Promise((ok, no) => {
    const x = job.xhr = new XMLHttpRequest();
    job.state = "uploading"; job.sent = 0; job.hist = []; renderJobs();
    x.open("PUT", fileUrl(job.path));
    x.upload.onprogress = e => { job.sent = e.loaded; tick(job); };
    x.onload = () => {
      let j = {}; try { j = JSON.parse(x.responseText); } catch {}
      if (x.status < 300) { job.sent = job.file.size; ok(j); }
      else no(Object.assign(new Error(j.error || "Upload failed (" + x.status + ")"), {fatal: x.status < 500}));
    };
    x.onerror = () => no(new Error("Connection lost"));
    x.onabort = () => no(new Error("Cancelled"));
    x.send(job.file);
  });
}
addEventListener("beforeunload", e => { if (active || jobs.some(j => j.state === "waiting")) e.preventDefault(); });
async function filesFromDrop(dt) {
  const entries = [...dt.items].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);  // must run before any await
  if (!entries.length) return [...dt.files].map(f => ({file: f, rel: f.name}));
  const out = [];
  const walk = async (entry, prefix) => {
    if (entry.isFile) out.push({file: await new Promise((ok, no) => entry.file(ok, no)), rel: prefix + entry.name});
    else if (entry.isDirectory) {
      const r = entry.createReader();
      for (let batch; (batch = await new Promise((ok, no) => r.readEntries(ok, no))).length;)
        for (const e of batch) await walk(e, prefix + entry.name + "/");
    }
  };
  for (const e of entries) await walk(e, "");
  return out;
}
const canDrop = e => !dragging && I.edit && tab === "files" && dir !== TRASH && e.dataTransfer.types.includes("Files");
addEventListener("dragenter", e => { if (canDrop(e)) { depth++; $("drop").hidden = false; } });
addEventListener("dragleave", e => { if (canDrop(e) && --depth <= 0) { depth = 0; $("drop").hidden = true; } });
addEventListener("dragover", e => { if (!dragging) e.preventDefault(); });
addEventListener("drop", async e => {
  e.preventDefault(); depth = 0; $("drop").hidden = true;
  if (canDrop(e)) queue(await filesFromDrop(e.dataTransfer));
});

// ---- Notes: threads with names, times, formatting, links and files ----
async function loadNotes() {
  const j = await api("/api/notes");
  try { localStorage.setItem("hs-notes-seen", String(Math.max(0, ...j.posts.map(p => p.t)))); } catch {}
  $("notesdot").hidden = true;
  $("myname").textContent = I.me.name || I.me.label;
  if (!$("newpost").firstChild) $("newpost").append(composer(null));
  const sig = JSON.stringify(j.posts) + j.me;
  if (sig === notesSig) return;
  notesSig = sig;
  const kids = id => j.posts.filter(p => p.parent === id);
  const last = r => Math.max(r.t, ...kids(r.id).map(p => p.t));
  const roots = j.posts.filter(p => !p.parent).sort((a, b) => last(b) - last(a));
  $("threads").replaceChildren(...roots.map(r => el("div", {className: "card thread"}, postEl(r, j.me), ...kids(r.id).map(p => postEl(p, j.me, true)), drafts[r.id] ||= replyBox(r.id))));
  $("nonotes").hidden = roots.length > 0;
}
function postEl(p, me, reply) {
  const body = el("div", {className: "body"});
  body.innerHTML = p.html;  // cleaned by the sharing computer before it was saved
  embeds(body);
  const vw = p.files.filter(f => VIEW.includes(kindOf(f.n)));
  const tools = el("div", {className: "row tools"}, btn("Copy text", e => copyText(body.innerText, e.target)));
  if (I.host || p.dev === me) tools.append(btn("Delete", act(async () => { if (confirm(reply ? "Delete this reply?" : "Delete this whole thread?")) await api("/api/notes/delete", {id: p.id}); }), "edit"));
  return el("div", {className: "post" + (reply ? " reply" : "")},
    el("div", {className: "phead"}, el("strong", {textContent: p.name}), p.name !== p.label ? el("span", {className: "mut", textContent: p.label}) : "",
      el("time", {className: "mut", textContent: "· " + ago(p.t), title: fmtDate(p.t)})),
    body, p.files.length ? el("div", {className: "atts"}, ...p.files.map(f => attEl(f, vw))) : "", tools);
}
function embeds(body) {
  let n = 0;
  body.querySelectorAll("a").forEach(a => {
    a.target = "_blank"; a.rel = "noopener noreferrer";
    let u; try { u = new URL(a.href); } catch { return; }
    if (n >= 5 || !/^https?:$/.test(u.protocol)) return;
    const host = u.hostname.replace(/^(www|m)\./, "");
    const yt = host === "youtu.be" ? u.pathname.slice(1) : host === "youtube.com" ? (u.searchParams.get("v") || (u.pathname.match(/^\/shorts\/([\w-]+)/) || [])[1]) : null;
    let node;
    if (yt && /^[\w-]{6,20}$/.test(yt)) node = el("iframe", {src: "https://www.youtube-nocookie.com/embed/" + yt, title: "YouTube video", loading: "lazy", allowFullscreen: true});
    else if (/\.(jpe?g|png|gif|webp|avif)$/i.test(u.pathname)) node = el("img", {src: u.href, alt: "", loading: "lazy", referrerPolicy: "no-referrer"});
    else node = el("a", {className: "lcard", href: u.href, target: "_blank", rel: "noopener noreferrer"}, "🔗 ", el("strong", {textContent: u.hostname}), el("span", {className: "mut", textContent: (u.pathname + u.search).slice(0, 50)}));
    body.append(el("div", {className: "embed"}, node)); n++;
  });
}
function attEl(f, vw) {
  const k = kindOf(f.n), viewable = VIEW.includes(k), a = el("a", {className: "att", href: fileUrl(f.path) + (viewable ? "" : "?dl=1"), title: f.n});
  if (!viewable) a.download = f.n;
  else a.onclick = e => { e.preventDefault(); openViewer(vw, vw.findIndex(v => v.path === f.path)); };
  const icon = el("span", {textContent: ICON[k]});
  if (k !== "video" && k !== "file") { const i = el("img", {src: "/thumb/" + enc(f.path), alt: "", loading: "lazy"}); i.onerror = () => i.replaceWith(icon); a.append(i); }  // e.g. iPhone HEIC
  else a.append(icon);
  a.append(el("span", {textContent: f.n}));
  return a;
}
function composer(parent) {
  const ed = el("div", {className: "editor", contentEditable: "true"});
  ed.setAttribute("role", "textbox"); ed.setAttribute("aria-multiline", "true"); ed.setAttribute("aria-label", parent ? "Write a reply" : "Write a new post");
  ed.dataset.ph = parent ? "Write a reply…" : "Start a new thread: write a message, paste a link, or attach files…";
  const mine = [], chips = el("div", {className: "row"}), pick = el("input", {type: "file", multiple: true, hidden: true});
  const post = el("button", {className: "pri", textContent: parent ? "Reply" : "Post"});
  const busy = () => mine.some(j => j.state !== "done");
  const showChips = () => {  // each attachment shows its own progress, retry and remove, right where it was attached
    chips.replaceChildren(...mine.map(j => {
      const x = Object.assign(btn("✕", () => { if (j.state !== "done") cancel(j); mine.splice(mine.indexOf(j), 1); showChips(); }), {ariaLabel: "Remove " + j.file.name});
      if (j.state === "done") return el("span", {className: "chip"}, "📎 " + j.final.split("/").pop(), x);
      if (j.state === "failed") return el("span", {className: "chip warn"}, "⚠️ " + j.file.name + ": " + j.err + " ", btn("Retry", () => retry(j)), x);
      return el("span", {className: "chip"}, "⏳ " + j.file.name + " · " + status(j), x);
    }));
    post.disabled = busy();
    post.title = busy() ? "Waiting for attachments to finish uploading" : "";
  };
  pick.onchange = () => {
    const fs = [...pick.files]; pick.value = "";
    mine.push(...queue(fs.map(f => ({file: f, rel: "Notes attachments/" + f.name})), "", showChips));
    showChips();
  };
  post.onclick = async () => {
    if (busy() || (!ed.textContent.trim() && !mine.length)) return;
    try {
      await api("/api/notes", {parent, html: ed.innerHTML, files: mine.map(j => j.final)});
      ed.innerHTML = ""; mine.length = 0; showChips(); notesSig = ""; loadNotes();
    } catch (e) { alert(e.message); }
  };
  ed.onkeydown = e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); post.click(); } };
  const cmd = (label, aria, run) => { const b = el("button", {type: "button", textContent: label, title: aria}); b.setAttribute("aria-label", aria); b.onmousedown = e => e.preventDefault(); b.onclick = () => { ed.focus(); run(); }; return b; };
  const exec = (c, v = null) => () => document.execCommand(c, false, v);
  const link = () => {
    const u = prompt("Link address:", "https://"); if (!u || u === "https://") return;
    const href = /^(https?:|mailto:)/i.test(u) ? u : "https://" + u, s = getSelection();
    if (s && !s.isCollapsed) document.execCommand("createLink", false, href);
    else document.execCommand("insertHTML", false, '<a href="' + esc(href) + '">' + esc(href) + "</a>&nbsp;");
  };
  const tb = el("div", {className: "tb"}, cmd("B", "Bold", exec("bold")), cmd("I", "Italic", exec("italic")), cmd("U", "Underline", exec("underline")),
    cmd("• List", "Bulleted list", exec("insertUnorderedList")), cmd("1. List", "Numbered list", exec("insertOrderedList")),
    cmd("❝ Quote", "Quote", exec("formatBlock", "blockquote")), cmd("🔗 Link", "Add a link", link), cmd("📎 Attach", "Attach files", () => pick.click()));
  return el("div", {}, tb, ed, chips, el("div", {className: "row"}, post, el("span", {className: "mut", textContent: "Ctrl+Enter sends"})), pick);
}
function replyBox(id) {
  const box = el("div", {className: "edit"});
  box.append(btn("Reply", () => { const c = composer(id); box.replaceChildren(c); c.querySelector(".editor").focus(); }));
  return box;
}
$("rename").onclick = act(async () => {
  const n = prompt("Your name (shown on your notes):", I.me.name || I.me.label);
  if (n !== null && n.trim()) await api("/api/me", {name: n});
});

// ---- viewer for pictures, videos, music, PDFs and text ----
function openViewer(list, idx) { if (!list.length) return; vlist = list; vi = Math.max(0, idx); $("viewer").hidden = false; showV(); $("vclose").focus(); }
function closeViewer() { $("vbody").replaceChildren(); $("viewer").hidden = true; }
function showV() {
  const it = vlist[vi], url = fileUrl(it.path), k = kindOf(it.n);
  $("vname").textContent = it.n + "   (" + (vi + 1) + " of " + vlist.length + ")";
  $("vdl").href = url + "?dl=1"; $("vdl").download = it.n;
  let node;
  if (k === "image") node = el("img", {src: url, alt: it.n});
  else if (k === "video") node = el("video", {src: url, controls: true, autoplay: true, playsInline: true});
  else if (k === "audio") {  // with its album art when the file has some
    const cover = el("img", {src: "/thumb/" + enc(it.path), alt: ""});
    cover.onerror = () => cover.remove();
    node = el("div", {className: "aud"}, cover, el("audio", {src: url, controls: true, autoplay: true}));
  }
  else if (k === "pdf") node = el("iframe", {src: url, title: it.n});
  else {
    node = el("pre", {textContent: "Loading…"});
    fetch(url, {headers: {Range: "bytes=0-262143"}}).then(r => r.text())
      .then(t => node.textContent = t + (it.s > 262144 ? "\n\n… (download the file to see the rest)" : ""))
      .catch(() => node.textContent = "Couldn't load this file.");
  }
  $("vbody").replaceChildren(node);
  $("vprev").hidden = $("vnext").hidden = vlist.length < 2;
  $("vframe").hidden = k !== "video" || !I.edit;
}

// ---- choosing a file's thumbnail: a picture, a frame from the video, or back to automatic ----
let tdItem = null;
function thumbMenu(it, path, kind) {
  tdItem = {it, path};
  $("tdtitle").textContent = "Thumbnail for " + it.n;
  $("tdframe").hidden = kind !== "video"; $("tdreset").hidden = !it.t;
  $("thumbdlg").showModal();
}
async function putThumb(path, blob) {
  const r = await fetch("/thumb/" + enc(path), {method: "PUT", body: blob});
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "Couldn't change the thumbnail.");
}
$("tdclose").onclick = () => $("thumbdlg").close();
$("tdpick").onclick = () => $("tdfile").click();
$("tdfile").onchange = act(async () => { const f = $("tdfile").files[0]; $("tdfile").value = ""; $("thumbdlg").close(); if (f) await putThumb(tdItem.path, f); });
$("tdreset").onclick = act(async () => { $("thumbdlg").close(); await api("/api/thumbreset", {path: tdItem.path}); });
$("tdframe").onclick = () => { $("thumbdlg").close(); const l = viewables(shown, dir); openViewer(l, l.findIndex(x => x.path === tdItem.path)); };
$("selthumb").onclick = () => { const it = items.find(i => i.n === [...sel][0]); thumbMenu(it, join(dir, it.n), kindOf(it.n)); };
$("vframe").onclick = async () => {  // pause on a frame, then press: that frame becomes the video's thumbnail
  const v = $("vbody").querySelector("video");
  if (!v || !v.videoWidth) return alert("Let the video load first, then pause on the frame you like.");
  const c = el("canvas", {width: v.videoWidth, height: v.videoHeight});
  c.getContext("2d").drawImage(v, 0, 0);
  try {
    await putThumb(vlist[vi].path, await new Promise(r => c.toBlob(r, "image/jpeg", 0.85)));
    $("vframe").textContent = "Saved ✓"; setTimeout(() => $("vframe").textContent = "Use this frame as thumbnail", 1500);
    lastSig = "";
  } catch (e) { alert(e.message); }
};
const step = d => { vi = (vi + d + vlist.length) % vlist.length; showV(); };
$("vprev").onclick = () => step(-1);
$("vnext").onclick = () => step(1);
$("vclose").onclick = closeViewer;
addEventListener("keydown", e => {
  if ($("viewer").hidden) return;
  if (e.key === "Escape") closeViewer();
  else if (e.key === "ArrowLeft") step(-1);
  else if (e.key === "ArrowRight") step(1);
});
let touchX = null;
$("vbody").ontouchstart = e => touchX = e.touches[0].clientX;
$("vbody").ontouchend = e => { if (touchX === null) return; const dx = e.changedTouches[0].clientX - touchX; touchX = null; if (Math.abs(dx) > 60 && vlist.length > 1) step(dx < 0 ? 1 : -1); };

// ---- start ----
setInterval(() => { if (!document.hidden) refresh(false); }, 4000);  // show what others do, and let waiting devices in
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(false); });  // phone unlocked: up to date at once

// Moving around HomeShare replaces the address instead of adding Back-button history: a tab without history is one
// the browser lets close itself, which keeps the tab tidy-up below working.
document.addEventListener("click", e => {
  const a = e.target.closest && e.target.closest('a[href^="#"]');
  if (a && !e.defaultPrevented && !e.ctrlKey && !e.metaKey && !e.shiftKey) { e.preventDefault(); location.replace(a.getAttribute("href")); }
});
// ---- one HomeShare tab per browser, however many get opened ----
// The browser remembers which tab is "the" HomeShare tab. Opening a tab, typing the link or refreshing makes that tab
// it; reloading by itself after HomeShare comes back doesn't. Every other HomeShare tab retires for good: it stops
// all checking (so it can't come back to life later), tries to close, and if the browser won't let it, says so.
let retired = false, retireLater = false;
const store = (fn, fallback) => { try { return fn(); } catch { return fallback; } };
const myTab = store(() => sessionStorage.getItem("hs-tab"), null) || Math.random().toString(36).slice(2);
const automatic = store(() => sessionStorage.getItem("hs-auto"), null);
store(() => { sessionStorage.setItem("hs-tab", myTab); sessionStorage.removeItem("hs-auto"); });
const channel = "BroadcastChannel" in window ? new BroadcastChannel("homeshare-tabs") : null;
function claimTab() {
  store(() => localStorage.setItem("hs-current", myTab));
  if (channel) channel.postMessage({current: myTab});
}
function retire() {
  if (retired) return;
  if (jobs.some(j => j.state === "uploading" || j.state === "waiting" || j.state === "retrying")) { retireLater = true; return; }  // finish uploads first
  retired = stopped = true;
  if (live) { live.close(); live = null; }
  closeViewer();
  window.close();
  setTimeout(() => {  // still here: the browser won't let this tab close itself (it has Back-button history)
    $("tabs").hidden = true; $("jobs").hidden = true;
    const go = el("button", {className: "pri", textContent: "Take me to HomeShare"});
    go.onclick = () => {
      if (channel) channel.postMessage({focus: true});  // ask the HomeShare tab to come forward (some browsers allow it)
      window.close();
      setTimeout(() => location.reload(), 400);  // can't close or switch tabs here: make this the HomeShare tab instead
    };
    document.querySelector("main").replaceChildren(el("section", {className: "card"},
      el("h2", {textContent: "HomeShare is open in another tab"}),
      el("p", {textContent: "HomeShare only runs in one tab at a time, so this one is paused. Your browser won't let this tab close itself: close it with the × on the tab, or press the button to use this tab instead."}),
      el("p", {}, go)));
  }, 300);
}
if (channel) channel.onmessage = e => {
  if (e.data.current && e.data.current !== myTab) retired ? window.close() : retire();  // leftovers try again each time
  if (e.data.focus && !retired) window.focus();
};
addEventListener("storage", e => { if (e.key === "hs-current" && e.newValue && e.newValue !== myTab) retire(); });
if (automatic && store(() => localStorage.getItem("hs-current"), myTab) !== myTab) retire();  // came back by itself, but another tab took over meanwhile
else claimTab();
(async () => { if (retired) return; try { await info(); } catch (e) { $("gate").textContent = e.message; } route(); if (!retired) watchLive(); })();
</script></body></html>"""

if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # there's no console window, so show the problem instead of vanishing
        alert(f"HomeShare couldn't start:\n\n{e}")
