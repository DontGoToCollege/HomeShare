"""Run: python test_homeshare.py"""
import http.cookiejar, io, json, re, shutil, socket, struct, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, zipfile
from pathlib import Path

import homeshare as hs

PHONE_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"


def test_helpers():
    dirty = ('<b onclick="x()">hi</b><script>alert(1)</script><a href="javascript:alert(1)">bad</a> see https://example.com/a?b=1.'
             '<div><img src=x onerror=alert(1)><strong>ok</strong></div><style>p{}</style><a href="https://x.org" title=t>x</a>')
    assert hs.clean_html(dirty) == ('<b>hi</b>bad see <a href="https://example.com/a?b=1">https://example.com/a?b=1</a>.'
                                    '<div><b>ok</b></div><a href="https://x.org">x</a>'), hs.clean_html(dirty)
    assert hs.clean_html("<i>unclosed <u>tags") == "<i>unclosed <u>tags</u></i>"
    assert hs.clean_html('<a href="&#106;avascript:alert(1)">x</a>') == "x"
    assert hs.device_label(PHONE_UA) == "Android phone (Chrome)"
    assert hs.device_label("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit Version/17.0 Mobile Safari/604.1") == "iPhone (Safari)"
    assert hs.is_home_ip("192.168.1.5") and hs.is_home_ip("10.0.0.2") and not hs.is_home_ip("8.8.8.8")
    h = hs.hash_pin("1234")
    assert hs.check_pin("1234", h) and not hs.check_pin("1235", h) and "1234" not in h


def test_all():
    root = Path(tempfile.mkdtemp())
    hs.CONFIG = root.parent / "homeshare-test-cfg.json"  # don't touch the real settings
    hs.CONFIG.unlink(missing_ok=True)
    srv = hs.Server(root, "test", 0, {"pin": "1111"})  # an older settings file with a plain-text PIN
    assert srv.pin_hash and "1111" not in hs.CONFIG.read_text() and srv.stop_hours == hs.STOP_DEFAULT
    srv.pin_hash = ""
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    root = srv.root
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    guest = f"http://{srv.ip}:{port}" if srv.ip != "127.0.0.1" else None  # same server, seen as another device

    def call(path, body=None, method=None, data=None, headers=None, via=None, full=False, opener=None):
        hdrs = dict(headers or {})
        if body is not None:
            data, hdrs["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request((via or base) + path, data=data, method=method, headers=hdrs)
        try:
            with (opener.open(req) if opener else urllib.request.urlopen(req)) as r:
                return (r.status, r.read(), r.headers) if full else (r.status, r.read())
        except urllib.error.HTTPError as e:
            return (e.code, e.read(), e.headers) if full else (e.code, e.read())

    def upload(path, content, **kw):  # the whole file in one request, as the page does
        return call("/f/" + urllib.parse.quote(path), data=content, method="PUT", **kw)[0]

    def phone():  # a separate device with its own cookies
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    # page security headers
    st, body, h = call("/", full=True)
    nonce = re.search(r'<script nonce="([^"]+)"', body.decode()).group(1)
    assert f"'nonce-{nonce}'" in h["Content-Security-Policy"] and "unsafe-inline" not in h["Content-Security-Policy"].split("script-src")[1].split(";")[0]
    assert h["X-Frame-Options"] == "DENY" and h["Referrer-Policy"] == "no-referrer" and "frame-ancestors 'none'" in h["Content-Security-Policy"]

    # uploads, never overwriting
    assert upload("hello.txt", b"hi") == 201
    st, body = call("/f/hello.txt", data=b"again", method="PUT")
    assert json.loads(body)["path"] == "hello (1).txt" and (root / "hello (1).txt").read_bytes() == b"again", "never overwrite"
    assert upload("bad:name?.txt", b"x") == 201 and (root / "bad_name_.txt").exists()
    assert upload("empty.txt", b"") == 201 and (root / "empty.txt").read_bytes() == b""
    assert upload("Album/2024/a.txt", b"aaa") == 201 and (root / "Album/2024/a.txt").exists()
    assert upload("big.bin", b"0123456789") == 201
    assert upload("../../escaped.txt", b"x") == 201 and not (root.parent / "escaped.txt").exists() and (root / "_/_/escaped.txt").exists()
    shutil.rmtree(root / "_")
    # an upload cut off partway leaves nothing half-written behind, and says so in the activity log
    cut = socket.create_connection(("127.0.0.1", port))
    cut.sendall(b"PUT /f/cut.bin HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 300000\r\n\r\n" + b"x" * 100_000)
    time.sleep(0.5)
    cut.close()
    time.sleep(0.5)
    assert not (root / "cut.bin").exists() and not [p for p in root.iterdir() if p.name.endswith(".part")]
    assert "stopped sending cut.bin partway (0.1 of 0.3 MB arrived)" in json.loads(call("/api/activity")[1])["items"][0]["what"]
    # big uploads note how fast they came; the speed test moves throwaway data both ways and keeps nothing
    assert upload("big-video.mp4", b"v" * (11 << 20)) == 201
    assert re.fullmatch(r"uploaded big-video\.mp4 \(11 MB in \d+ s, [\d.]+ MB/s\)", json.loads(call("/api/activity")[1])["items"][0]["what"])
    before = sorted(p.name for p in root.iterdir())
    st, body = call("/api/speedtest", data=bytes(3 << 20), method="PUT")
    assert st == 200 and json.loads(body)["secs"] >= 0
    assert len(call("/api/speedtest?mb=2")[1]) == 2 << 20 and sorted(p.name for p in root.iterdir()) == before
    (root / "big-video.mp4").unlink()
    # disk-space guard
    hs.RESERVE, saved = 1 << 60, hs.RESERVE
    assert upload("huge.bin", b"x") == 403
    hs.RESERVE = saved
    # lists are never cached (iPhone Safari would otherwise show stale files)
    assert call("/api/list?dir=", full=True)[2]["Cache-Control"] == "no-store"
    # the held-open connection pages use to notice HomeShare closing
    with urllib.request.urlopen(base + "/api/live", timeout=5) as r:
        assert r.headers["Content-Type"] == "text/event-stream" and r.read(21) == b"retry: 2000\n\n: here\n\n"
        assert srv.host_tabs == 1, "a HomeShare tab on this computer is counted, so startup can reuse it"
    for _ in range(20):  # counted off again once the tab goes
        if srv.host_tabs == 0:
            break
        time.sleep(0.1)
    # startup opens a tab only when none is waiting to reconnect
    opened = []
    hs.open_browser, real_open = opened.append, hs.open_browser
    srv.host_tabs = 1
    hs.open_unless_tab_waiting(srv, "http://x/", wait=1)
    srv.host_tabs = 0
    hs.open_unless_tab_waiting(srv, "http://x/", wait=1)
    hs.open_browser = real_open
    assert opened == ["http://x/"]

    # video seeking needs byte ranges
    st, body, h = call("/f/big.bin", headers={"Range": "bytes=2-4"}, full=True)
    assert st == 206 and body == b"234" and h["Content-Range"] == "bytes 2-4/10"
    assert call("/f/big.bin", headers={"Range": "bytes=-3"})[1] == b"789"
    assert call("/f/big.bin", headers={"Range": "bytes=50-"})[0] == 416
    # "the rest of the file" comes back one piece at a time, so a paused video can't hog a connection
    (root / "movie.mp4").write_bytes(b"v" * (hs.RANGE_PIECE + 1000))
    st, body, h = call("/f/movie.mp4", headers={"Range": "bytes=0-"}, full=True)
    assert st == 206 and len(body) == hs.RANGE_PIECE and h["Content-Range"] == f"bytes 0-{hs.RANGE_PIECE - 1}/{hs.RANGE_PIECE + 1000}"
    assert len(call("/f/movie.mp4", headers={"Range": f"bytes={hs.RANGE_PIECE}-"})[1]) == 1000
    assert len(call("/f/movie.mp4")[1]) == hs.RANGE_PIECE + 1000, "a normal download still gets the whole file"
    assert hs.Handler.timeout, "stalled connections must time out"

    # zip downloads
    form = urllib.parse.urlencode([("dir", ""), ("n", "Album"), ("n", "hello.txt")]).encode()
    with zipfile.ZipFile(io.BytesIO(call("/zip", data=form)[1])) as z:
        assert sorted(z.namelist()) == ["Album/2024/a.txt", "hello.txt"] and z.read("hello.txt") == b"hi"
    assert call("/zip", data=urllib.parse.urlencode([("dir", "Album"), ("n", "../hello.txt")]).encode())[0] == 403

    assert call("/api/mkdir", {"path": "Photos"})[0] == 200
    assert call("/api/mkdir", {"path": "Photos"})[0] == 409
    assert call("/api/move", {"path": "hello.txt", "to": "Photos/hello.txt"})[0] == 200
    assert call("/api/move", {"path": "Photos", "to": "Photos/inner"})[0] == 403
    assert call("/api/move", {"path": "bad_name_.txt", "to": "hello (1).txt"})[0] == 409

    # nothing outside the shared folder; other websites can't drive it
    assert call("/f/..%2F..%2Fsecret.txt")[0] == 403
    assert call("/api/list?dir=..")[0] == 403
    assert call("/api/move", {"path": "bad_name_.txt", "to": "../escaped.txt"})[0] == 403
    assert call("/api/delete", {"path": ""})[0] == 403
    assert call("/api/mkdir", data=b'{"path":"x"}', headers={"Content-Type": "text/plain"})[0] == 403
    assert call("/api/info", headers={"Host": "evil.example"})[0] == 403

    # trash
    assert call("/api/delete", {"path": "Photos"})[0] == 200 and (root / ".trash/Photos/hello.txt").exists()
    assert call("/api/restore", {"name": "Photos"})[0] == 200 and (root / "Photos/hello.txt").exists()
    assert call("/api/restore", {"name": "../hello (1).txt"})[0] == 403
    assert call("/api/delete", {"path": "empty.txt"})[0] == 200
    assert call("/api/emptytrash", {})[0] == 200 and not (root / ".trash").exists()

    # notes: rich text is cleaned, threads and replies, attachments
    assert call("/api/notes", {"html": "<b>Plan</b><script>x</script> https://youtu.be/abc12345", "files": ["big.bin"]})[0] == 200
    posts = json.loads(call("/api/notes")[1])["posts"]
    root_post = posts[0]
    assert root_post["html"] == '<b>Plan</b> <a href="https://youtu.be/abc12345">https://youtu.be/abc12345</a>'
    assert root_post["files"] == [{"path": "big.bin", "n": "big.bin", "s": 10}] and root_post["dev"] == "host"
    assert call("/api/notes", {"parent": root_post["id"], "html": "sounds good"})[0] == 200
    assert call("/api/notes", {"parent": "nope", "html": "x"})[0] == 404
    assert call("/api/notes", {"html": "<p> </p>"})[0] == 403
    assert call("/api/notes", {"html": "x", "files": ["../../etc/passwd"]})[0] == 403
    assert (root / hs.NOTES_FILE).exists() and len(json.loads((root / hs.NOTES_FILE).read_text())) == 2

    # thumbnails and QR code
    if hs.Image:
        hs.Image.new("RGBA", (800, 600), (255, 0, 0, 128)).save(root / "pic.png")
        st, body, h = call("/thumb/pic.png?v=1", full=True)
        assert st == 200 and h["Content-Type"] == "image/jpeg" and hs.Image.open(io.BytesIO(body)).size == (360, 270)
        (root / "fake.jpg").write_bytes(b"not an image")
        assert call("/thumb/fake.jpg")[0] == 415
        # previews for more than photos: a text snapshot, an Office file's saved preview, never a guess for videos
        (root / "notes.txt").write_text("hello\nworld")
        assert call("/thumb/notes.txt")[0] == 200
        pic = io.BytesIO()
        hs.Image.new("RGB", (200, 100), "blue").save(pic, "JPEG")
        with zipfile.ZipFile(root / "report.docx", "w") as z:
            z.writestr("docProps/thumbnail.jpeg", pic.getvalue())
        assert call("/thumb/report.docx")[0] == 200
        with zipfile.ZipFile(root / "plain.docx", "w") as z:
            z.writestr("word/document.xml", "<x/>")
        assert call("/thumb/plain.docx")[0] == 415 and call("/thumb/plain.docx")[0] == 415, "no preview, and it's remembered"
        assert call("/thumb/movie.mp4")[0] == 415
        if hs.mutagen:  # album art inside a music file
            from mutagen.id3 import APIC, ID3
            (root / "song.mp3").write_bytes(b"\0" * 1000)
            tag = ID3()
            tag.add(APIC(mime="image/jpeg", type=3, data=pic.getvalue()))
            tag.save(root / "song.mp3")
            st, body = call("/thumb/song.mp3")
            assert st == 200 and hs.Image.open(io.BytesIO(body)).size == (200, 100)
        if hs.pdfium:  # first page of a PDF
            hs.Image.new("RGB", (400, 600), "red").save(root / "doc.pdf")
            st, body = call("/thumb/doc.pdf")
            assert st == 200 and hs.Image.open(io.BytesIO(body)).size[1] == 360
        # choosing a thumbnail: shows everywhere, follows the file when it's renamed, can go back to automatic
        assert call("/thumb/movie.mp4", data=pic.getvalue(), method="PUT")[0] == 201
        assert call("/thumb/movie.mp4")[0] == 200
        assert "t" in next(i for i in json.loads(call("/api/list?dir=")[1])["items"] if i["n"] == "movie.mp4")
        assert call("/api/move", {"path": "movie.mp4", "to": "clip.mp4"})[0] == 200 and call("/thumb/clip.mp4")[0] == 200
        assert call("/thumb/clip.mp4", data=b"not a picture", method="PUT")[0] == 403
        assert call("/api/thumbreset", {"path": "clip.mp4"})[0] == 200 and call("/thumb/clip.mp4")[0] == 415
    else:
        assert call("/thumb/movie.mp4", data=b"x", method="PUT")[0] == 403
    if hs.segno:
        st, body = call("/api/qr.svg")
        assert st == 200 and body.startswith(b"<svg") and b"xmlns" in body

    info = json.loads(call("/api/info")[1])
    assert info["links"][0] == f"http://test.local:{port}" and info["link_code"] == "" and info["need"] is None, "plain link by default"
    assert re.fullmatch(r"\d+\.\d+\.\d+", info["version"]) and info["version"] == hs.VERSION
    # the host picks the link: name and an optional code
    assert call("/api/link", {"name": "Family Files", "code": "Beach Trip"})[0] == 200
    info = json.loads(call("/api/info")[1])
    assert info["links"][0] == f"http://family-files.local:{port}/beach-trip" and info["link_code"] == "beach-trip"
    assert srv.name == "family-files" and json.loads(hs.CONFIG.read_text())["key"] == "beach-trip"
    assert call("/api/link", {"code": "api"})[0] == 403 and srv.key == "beach-trip"

    if guest:
        a = phone()
        g = lambda path, **kw: call(path, via=guest, opener=a, headers={"User-Agent": PHONE_UA}, **kw)
        # without the link's code: nothing but the page
        assert g("/")[0] == 200
        assert json.loads(g("/api/info")[1])["need"] == "link"
        assert g("/api/list?dir=")[0] == 401 and g("/f/big.bin")[0] == 401
        assert g("/beach")[0] == 200 and json.loads(g("/api/info")[1])["need"] == "link"
        # with it: in, and remembered as a device
        assert g("/Beach-Trip")[0] == 200 and g("/api/list?dir=")[0] == 200
        old = phone()
        call("/k/beach-trip", via=guest, opener=old)
        assert call("/api/list?dir=", via=guest, opener=old)[0] == 200, "links in the older /k/ style still work"
        # someone guessing codes gets locked out, even if they then hit the right one
        e = phone()
        for i in range(10):
            call(f"/guess{i}", via=guest, opener=e)
        call("/beach-trip", via=guest, opener=e)
        assert json.loads(call("/api/info", via=guest, opener=e)[1])["need"] == "link"
        srv.code_fails.clear()
        call("/beach-trip", via=guest, opener=e)
        assert call("/api/list?dir=", via=guest, opener=e)[0] == 200
        srv.devices = {k: v for k, v in srv.devices.items() if v["label"] == "Android phone (Chrome)"}  # just the phone, for the checks below
        assert len(srv.devices) == 1 and next(iter(srv.devices.values()))["label"] == "Android phone (Chrome)"
        assert g("/api/settings", body={"lan": False})[0] == 403 and g("/api/devices")[0] == 403
        assert g("/api/activity/clear", body={})[0] == 403 and g("/api/activity/remove", body={"id": 1})[0] == 403
        assert "folder" not in json.loads(g("/api/info")[1])
        # names and notes
        assert g("/api/me", body={"name": "Sam's phone"})[0] == 200
        assert g("/api/notes", body={"parent": root_post["id"], "html": "from the phone"})[0] == 200
        mine = json.loads(g("/api/notes")[1])["posts"][-1]
        assert mine["name"] == "Sam's phone" and mine["label"] == "Android phone (Chrome)"
        assert g("/api/notes/delete", body={"id": root_post["id"]})[0] == 403, "can't delete someone else's thread"
        assert g("/api/notes/delete", body={"id": mine["id"]})[0] == 200
        # view-only
        assert call("/api/settings", {"guest_edit": False})[0] == 200
        assert upload("g.txt", b"g", via=guest, opener=a) == 403
        assert g("/api/notes", body={"html": "x"})[0] == 403 and g("/api/notes")[0] == 200
        assert g("/f/big.bin")[1] == b"0123456789", "view-only can still download"
        assert g("/thumb/big.bin", data=b"x", method="PUT")[0] == 403 and g("/api/thumbreset", body={"path": "big.bin"})[0] == 403
        assert call("/api/settings", {"guest_edit": True})[0] == 200
        assert upload("g.txt", b"g", via=guest, opener=a) == 201
        # PIN (stored hashed), QR code that skips it
        assert call("/api/settings", {"pin": "2468"})[0] == 200 and "2468" not in hs.CONFIG.read_text()
        assert json.loads(g("/api/info")[1])["need"] == "pin" and g("/api/list?dir=")[0] == 401
        assert g("/api/login", body={"pin": "0000"})[0] == 403
        assert g("/api/login", body={"pin": "2468"})[0] == 200 and g("/api/list?dir=")[0] == 200
        b = phone()
        assert call(srv.qr_url().split(str(port), 1)[1], via=guest, opener=b)[0] == 200
        assert call("/api/list?dir=", via=guest, opener=b)[0] == 200, "QR code should skip the PIN"
        assert call("/api/settings", {"qr_pin": False})[0] == 200
        c = phone()
        call(f"/{srv.key}?p={srv.pin_cookie()[:16]}", via=guest, opener=c)
        assert call("/api/list?dir=", via=guest, opener=c)[0] == 401
        assert call("/api/settings", {"pin": ""})[0] == 200
        # approval
        assert call("/api/settings", {"approve": True})[0] == 200
        d = phone()
        call(f"/{srv.key}", via=guest, opener=d, headers={"User-Agent": PHONE_UA})
        assert json.loads(call("/api/info", via=guest, opener=d)[1])["need"] == "approval"
        assert call("/api/list?dir=", via=guest, opener=d)[0] == 401
        assert call("/api/me", {"name": "Mom's tablet"}, via=guest, opener=d)[0] == 200
        devs = json.loads(call("/api/devices")[1])["devices"]
        pending = [x for x in devs if x["status"] == "pending"]
        assert len(pending) == 1 and pending[0]["name"] == "Mom's tablet" and json.loads(call("/api/info")[1])["pending"] == 1
        assert g("/api/list?dir=")[0] == 200, "devices let in before approval was switched on keep working"
        assert call("/api/device", {"id": pending[0]["id"], "action": "allow"})[0] == 200
        assert call("/api/list?dir=", via=guest, opener=d)[0] == 200
        assert call("/api/device", {"id": pending[0]["id"], "action": "block"})[0] == 200
        assert json.loads(call("/api/info", via=guest, opener=d)[1])["need"] == "blocked"
        assert call("/api/settings", {"approve": False})[0] == 200
        assert json.loads(call("/api/info", via=guest, opener=d)[1])["need"] == "blocked", "removed stays removed"
        # pause on an unfamiliar network
        srv.net_ok = False
        assert json.loads(g("/api/info")[1])["need"] == "paused" and call("/api/list?dir=")[0] == 200
        srv.net_ok = True
        # "Make a secure link" cuts everyone off; removing the code opens it to the whole Wi-Fi
        assert call("/api/newkey", {})[0] == 200 and len(srv.key) == 10 and srv.require_key
        assert json.loads(g("/api/info")[1])["need"] == "link"
        g(f"/{srv.key}")
        assert g("/api/list?dir=")[0] == 200
        assert call("/api/link", {"code": ""})[0] == 200 and json.loads(call("/api/info")[1])["links"][0] == f"http://family-files.local:{port}"
        assert call("/api/list?dir=", via=guest, opener=phone())[0] == 200
        # PIN lockout after 5 wrong tries (by address)
        call("/api/settings", {"pin": "9999"})
        for _ in range(5):
            g("/api/login", body={"pin": "0000"})
        st, body = g("/api/login", body={"pin": "9999"})
        assert st == 403 and b"15 minutes" in body
        srv.fails.clear()
        call("/api/settings", {"pin": ""})
        # the off switch locks other devices out
        assert call("/api/settings", {"lan": False})[0] == 200
        assert g("/")[0] == 403 and call("/")[0] == 200
        assert call("/api/settings", {"lan": True})[0] == 200
        acts = " | ".join(f"{x['who']} {x['what']}" for x in json.loads(call("/api/activity")[1])["items"])
        for expect in ("Sam's phone uploaded g.txt", "connected for the first time", "asked to connect",
                       "removed Mom's tablet", "made a new link", "locked out"):
            assert expect in acts, (expect, acts)

    # Recent activity: file entries carry size and type; Remove file keeps the line, Reclaim puts the file back
    acts = lambda: json.loads(call("/api/activity")[1])["items"]
    assert upload("Photos/beach.jpg", b"b" * 1234) == 201
    entry = next(a for a in acts() if a["what"] == "uploaded Photos/beach.jpg")
    assert entry["size"] == 1234 and entry["path"] == "Photos/beach.jpg" and not entry["dir"] and entry["exists"]
    assert call("/api/activity/remove", {"id": entry["id"]})[0] == 200
    assert not (root / "Photos/beach.jpg").exists()
    entry = next(a for a in acts() if a["id"] == entry["id"])
    assert entry["removed"] and entry["in_trash"] and not entry["exists"], "the log line stays, marked removed"
    assert call("/api/activity/remove", {"id": entry["id"]})[0] == 404, "can't remove what isn't there"
    assert upload("Photos/beach.jpg", b"new") == 201  # something new took its place meanwhile
    assert call("/api/activity/reclaim", {"id": entry["id"]})[0] == 200
    assert (root / "Photos/beach (1).jpg").read_bytes() == b"b" * 1234 and (root / "Photos/beach.jpg").read_bytes() == b"new"
    entry = next(a for a in acts() if a["id"] == entry["id"])
    assert "removed" not in entry and entry["path"] == "Photos/beach (1).jpg" and entry["exists"]
    # a deleted file's line can be reclaimed straight away; once the trash is emptied it can't
    assert call("/api/delete", {"path": "Photos/beach.jpg"})[0] == 200
    gone = acts()[0]
    assert gone["what"] == "deleted Photos/beach.jpg" and gone["removed"] and gone["in_trash"] and gone["size"] == 3
    assert call("/api/activity/reclaim", {"id": gone["id"]})[0] == 200 and (root / "Photos/beach.jpg").read_bytes() == b"new"
    assert call("/api/activity/remove", {"id": gone["id"]})[0] == 200 and call("/api/emptytrash", {})[0] == 200
    assert not next(a for a in acts() if a["id"] == gone["id"])["in_trash"]
    assert call("/api/activity/reclaim", {"id": gone["id"]})[0] == 403
    assert call("/api/activity/clear", {})[0] == 200 and acts() == []

    # auto-stop timer
    assert call("/api/settings", {"stop_hours": 2})[0] == 200 and srv.stop_at and json.loads(hs.CONFIG.read_text())["stop_hours"] == 2
    assert call("/api/settings", {"stop_hours": 0})[0] == 200 and srv.stop_at is None

    # mDNS
    q = struct.pack("!6H", 0, 0, 1, 0, 0, 0) + b"\x0cfamily-files\x05local\x00" + struct.pack("!HH", 1, 1)
    r = hs.mdns_reply(q, "family-files", "192.168.1.5")
    assert r and r.endswith(socket.inet_aton("192.168.1.5"))
    assert hs.mdns_reply(q, "someone-else", "192.168.1.5") is None
    assert hs.mdns_reply(struct.pack("!6H", 0, 0, 1, 0, 0, 0) + b"\xc0\x0c", "x", "1.2.3.4") is None

    assert call("/api/stop", {})[0] == 200
    th.join(5)
    assert not th.is_alive(), "stop button must shut the server down"
    hs.CONFIG.unlink(missing_ok=True)
    return guest is not None


if __name__ == "__main__":
    test_helpers()
    had_guest = test_all()
    print("all tests passed", "(with Pillow + segno)" if hs.Image and hs.segno else "(without optional extras)",
          "" if had_guest else "-- no network, so the other-device checks were skipped")
