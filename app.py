from __future__ import annotations

import cgi
import hashlib
import html
import io
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_IMAGE_SIZE_MB = 8
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class MatchResult:
    source: str
    title: str
    url: str


def image_fingerprint(image_bytes: bytes) -> str:
    """Compute a deterministic content fingerprint for uploaded bytes."""
    return hashlib.sha256(image_bytes).hexdigest()


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            attrs_dict = dict(attrs)
            self._href = attrs_dict.get("href")
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            text = " ".join("".join(self._text_parts).split())
            self.links.append((self._href, text))
            self._href = None
            self._text_parts = []


def google_reverse_image_search(image_bytes: bytes, filename: str) -> list[MatchResult]:
    boundary = "----WebKitFormBoundaryCodexScanner"
    body = io.BytesIO()
    fields = {
        "image_content": "",
        "filename": filename,
        "hl": "en",
    }
    for k, v in fields.items():
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())

    body.write(f"--{boundary}\r\n".encode())
    body.write(
        f'Content-Disposition: form-data; name="encoded_image"; filename="{filename}"\r\n'.encode()
    )
    body.write(b"Content-Type: application/octet-stream\r\n\r\n")
    body.write(image_bytes)
    body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        "https://www.google.com/searchbyimage/upload",
        data=body.getvalue(),
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "Mozilla/5.0",
        },
    )

    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
    response = opener.open(req, timeout=20)
    result_url = response.geturl()
    html_page = urllib.request.urlopen(
        urllib.request.Request(result_url, headers={"User-Agent": "Mozilla/5.0"}),
        timeout=20,
    ).read().decode("utf-8", errors="ignore")

    parser = LinkParser()
    parser.feed(html_page)
    results: list[MatchResult] = []
    for href, text in parser.links:
        if not href or len(text) < 8:
            continue
        if href.startswith("/"):
            href = f"https://www.google.com{href}"
        if not href.startswith("http"):
            continue
        if any(bad in href for bad in ["accounts.google", "google.com/preferences"]):
            continue
        results.append(MatchResult(source="Google Lens", title=text[:120], url=href))
        if len(results) >= 20:
            break
    return dedupe_by_url(results)


def dedupe_by_url(items: list[MatchResult]) -> list[MatchResult]:
    out: list[MatchResult] = []
    seen: set[str] = set()
    for item in items:
        clean = re.sub(r"#.*$", "", item.url)
        if clean in seen:
            continue
        seen.add(clean)
        out.append(item)
    return out


def social_links_only(items: list[MatchResult]) -> list[MatchResult]:
    social_domains = [
        "instagram.com",
        "facebook.com",
        "x.com",
        "twitter.com",
        "tiktok.com",
        "linkedin.com",
        "youtube.com",
        "reddit.com",
        "pinterest.com",
    ]
    return [i for i in items if any(d in i.url.lower() for d in social_domains)]


def render_page(error: str = "", phash: str = "", total: int = 0, matches: list[MatchResult] | None = None) -> bytes:
    items_html = ""
    if matches is not None:
        if matches:
            lis = []
            for m in matches:
                lis.append(
                    f'<li><a href="{html.escape(m.url)}" target="_blank" rel="noopener noreferrer">{html.escape(m.title)}</a> <small>({html.escape(m.source)})</small></li>'
                )
            items_html = "<ul>" + "".join(lis) + "</ul>"
        else:
            items_html = "<p>No social links found from this scan.</p>"

    body = f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Image Exposure Scanner</title>
<style>
body{{font-family:system-ui;background:#0f172a;color:#e2e8f0;margin:0}}main{{max-width:760px;margin:2rem auto;padding:0 1rem}}.card{{background:#111827;border:1px solid #334155;border-radius:10px;padding:1rem;margin:1rem 0}}input,button{{display:block;width:100%;margin-top:.6rem;padding:.6rem;border-radius:8px;border:1px solid #475569}}button{{background:#22c55e;color:#052e16;font-weight:700}}a{{color:#93c5fd}}.error{{color:#fca5a5}}
</style></head>
<body><main>
<h1>Image Exposure Scanner</h1>
<p>Upload your photo and check where visually similar images appear publicly on social media.</p>
<form class=\"card\" action=\"/scan\" method=\"post\" enctype=\"multipart/form-data\">
<label for=\"photo\">Select image (JPG/PNG/WEBP, max {MAX_IMAGE_SIZE_MB}MB)</label>
<input id=\"photo\" name=\"photo\" type=\"file\" required>
<button type=\"submit\">Scan Public Internet</button>
</form>
{f'<p class="error">{html.escape(error)}</p>' if error else ''}
{f'<section class="card"><h2>Scan completed</h2><p><strong>Image fingerprint:</strong> <code>{html.escape(phash)}</code></p><p><strong>Total public matches:</strong> {total}</p><p>This uses public reverse-image results and may miss private pages.</p></section>' if phash else ''}
{f'<section class="card"><h2>Likely social media matches</h2>{items_html}</section>' if matches is not None else ''}
</main></body></html>"""
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        body = render_page()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/scan":
            self.send_error(404)
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            },
        )
        if "photo" not in form:
            self.respond(render_page(error="Please upload an image first."))
            return

        photo = form["photo"]
        filename = photo.filename or "upload"
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ALLOWED_EXTS:
            self.respond(render_page(error="Unsupported file type. Use JPG, PNG, or WEBP."))
            return

        image_bytes = photo.file.read()
        if len(image_bytes) > MAX_IMAGE_SIZE_MB * 1024 * 1024:
            self.respond(render_page(error=f"Image must be <= {MAX_IMAGE_SIZE_MB} MB."))
            return

        fingerprint = image_fingerprint(image_bytes)
        try:
            all_results = google_reverse_image_search(image_bytes, filename)
        except Exception:
            all_results = []
        social = social_links_only(all_results)
        self.respond(render_page(phash=fingerprint, total=len(all_results), matches=social))

    def respond(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8000), Handler)
    print("Listening on http://127.0.0.1:8000")
    server.serve_forever()
