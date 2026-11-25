"""Read sheet music out of a PDF with homr, over HTTP.

This is what lets the Piano Fingering app open a scanned score on its own.
The app does no recognition itself: homr is AGPL-3.0, so it runs here, as a
separate program the app calls over the network, and this service is AGPL-3.0
with it. The app contains none of it.

One request, one PDF, answered a page at a time:

    POST /v1/recognise            body: the PDF (Content-Type: application/pdf)
    -> 200 application/x-ndjson, one JSON object per line as each is ready:
       {"type": "waiting"}                         another score is being read
       {"type": "start", "pages": 3, "source": "https://..."}
       {"type": "page", "page": 1, "pages": 3, "musicxml": "<?xml ..."}
       ...
       {"type": "done"}
     or{"type": "error", "message": "...", "page": 2}   and nothing after it

    GET /health                   {"ok": true, "homr": "0.7.1"}
    GET /source                   where this service's source can be had

Why a page at a time, and why each page is rendered here rather than handed
to homr as a PDF:

- Given several pages at once, homr merges their MusicXML with a parser that
  drops comments, and the comments are where it writes each note's position
  on the page (`<!-- imgpos: x, y -->`). The app needs those positions to
  draw a fingering over its note, so each page is recognised on its own.
- Given a PDF, homr renders it and then crops the picture to the paper before
  recognising it, and the positions it writes are then in the cropped
  picture. The app places them on the whole page. Given an image instead,
  homr crops internally and maps every position back to the image it was
  given. So each page is rendered here, whole, at the 300 dpi the app assumes
  (RECOGNITION_DPI in the app's src/score/imageGeometry.ts).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import pypdfium2 as pdfium

# Must match RECOGNITION_DPI in the app, which turns these pixels back into
# the page it draws.
RENDER_DPI = 300

# A recogniser takes a rendered page and whether to look for a title, and
# gives back that page's MusicXML. homr in production; a stand-in in tests.
Recogniser = Callable[[Path, bool], str]


class RecognitionError(Exception):
    """A page homr could not read, with what it said about it."""


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    # Pages the browser may call from. A check on Origin keeps other websites
    # from spending this machine's time; it is not access control, since
    # anything that is not a browser can send whatever Origin it likes.
    allowed_origins: frozenset[str] = field(
        default_factory=lambda: frozenset({"http://localhost:5173", "http://127.0.0.1:5173"})
    )
    max_bytes: int = 25 * 1024 * 1024
    # About 17 seconds a page on a laptop CPU: twenty is six minutes.
    max_pages: int = 20
    page_timeout: int = 300
    homr: str = "homr"
    # Where this service's source can be had. The AGPL asks that people who
    # use it over a network be offered the source, so this must be set to a
    # public copy before the service is opened to anyone but yourself.
    source_url: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        defaults = cls()
        origins = env.get("PIANO_OMR_ORIGINS")
        return cls(
            host=env.get("PIANO_OMR_HOST", defaults.host),
            port=int(env.get("PIANO_OMR_PORT", defaults.port)),
            allowed_origins=(
                frozenset(o.strip().rstrip("/") for o in origins.split(",") if o.strip())
                if origins
                else defaults.allowed_origins
            ),
            max_bytes=int(float(env.get("PIANO_OMR_MAX_MB", 25)) * 1024 * 1024),
            max_pages=int(env.get("PIANO_OMR_MAX_PAGES", defaults.max_pages)),
            page_timeout=int(env.get("PIANO_OMR_PAGE_TIMEOUT", defaults.page_timeout)),
            homr=env.get("PIANO_OMR_HOMR", default_homr()),
            source_url=env.get("PIANO_OMR_SOURCE_URL") or None,
        )


def default_homr() -> str:
    """homr installed next to this Python, else whatever is on the PATH."""
    beside = Path(sys.executable).with_name("homr")
    return str(beside) if beside.exists() else "homr"


def homr_version() -> str:
    try:
        return metadata.version("homr")
    except metadata.PackageNotFoundError:
        return "unknown"


def render_page(document: pdfium.PdfDocument, index: int, folder: Path) -> Path:
    """One page, whole and uncropped, as a PNG at RENDER_DPI."""
    page = document[index]
    try:
        image = page.render(scale=RENDER_DPI / 72).to_pil().convert("RGB")
    finally:
        page.close()
    path = folder / f"page-{index + 1:03d}.png"
    image.save(path)
    return path


def homr_recogniser(settings: Settings) -> Recogniser:
    def recognise(image: Path, with_title: bool) -> str:
        command = [settings.homr, str(image)]
        # The title is on the first page; reading it on every other page is
        # time spent on OCR for nothing.
        if not with_title:
            command.append("--no-title")
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=settings.page_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RecognitionError(
                f"homr took longer than {settings.page_timeout} seconds on this page."
            ) from error
        except FileNotFoundError as error:
            raise RecognitionError(f"homr was not found at {settings.homr!r}.") from error

        output = image.with_suffix(".musicxml")
        if result.returncode != 0 or not output.exists():
            said = [line for line in result.stderr.strip().splitlines() if line.strip()]
            raise RecognitionError(said[-1] if said else "homr did not write a result.")
        return output.read_text(encoding="utf-8")

    return recognise


# homr is memory-hungry and uses every core it is given; two scores read at
# once would each take twice as long and risk running out of memory, so the
# second waits for the first.
_one_at_a_time = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "piano-omr"
    settings: Settings
    recognise: Recogniser

    # ---- CORS ---------------------------------------------------------------

    def _origin(self) -> str | None:
        origin = self.headers.get("Origin")
        return origin.rstrip("/") if origin else None

    def _origin_allowed(self) -> bool:
        origin = self._origin()
        # No Origin header: not a browser page (curl, a script). Allowed, so
        # the service can be tried from a terminal.
        return origin is None or origin in self.settings.allowed_origins

    def _cors_headers(self) -> None:
        origin = self._origin()
        if origin and origin in self.settings.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self) -> None:  # noqa: N802 - the name http.server calls
        if not self._origin_allowed():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "This origin may not use the service."})
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- Plain answers --------------------------------------------------------

    def _send_json(self, status: HTTPStatus, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "homr": homr_version()})
        elif path == "/source":
            if self.settings.source_url:
                self._send_json(HTTPStatus.OK, {"source": self.settings.source_url})
            else:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "No public source address is configured (PIANO_OMR_SOURCE_URL)."},
                )
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    # ---- Recognition ----------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/v1/recognise":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        if not self._origin_allowed():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "This origin may not use the service."})
            return

        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Send the PDF with a length."})
            return
        size = int(length)
        if size > self.settings.max_bytes:
            # The body is not read, so the connection cannot be reused.
            self.close_connection = True
            megabytes = self.settings.max_bytes // (1024 * 1024)
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": f"The PDF is larger than {megabytes} MB."},
            )
            return
        pdf = self.rfile.read(size)
        if not pdf.startswith(b"%PDF"):
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "That is not a PDF."})
            return

        try:
            document = pdfium.PdfDocument(pdf)
        except pdfium.PdfiumError:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "The PDF could not be opened."})
            return

        try:
            pages = len(document)
            if pages == 0:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "The PDF has no pages."})
                return
            if pages > self.settings.max_pages:
                self._send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {
                        "error": f"The PDF has {pages} pages; this service reads up to "
                        f"{self.settings.max_pages} at a time."
                    },
                )
                return
            self._stream(document, pages)
        finally:
            document.close()

    def _stream(self, document: pdfium.PdfDocument, pages: int) -> None:
        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            if not _one_at_a_time.acquire(blocking=False):
                self._line({"type": "waiting"})
                _one_at_a_time.acquire()
            try:
                self._line({"type": "start", "pages": pages, "source": self.settings.source_url})
                with tempfile.TemporaryDirectory(prefix="piano-omr-") as work:
                    folder = Path(work)
                    for index in range(pages):
                        began = time.monotonic()
                        try:
                            image = render_page(document, index, folder)
                            musicxml = self.recognise(image, index == 0)
                            self.log_message(
                                "page %d/%d read in %.1fs", index + 1, pages, time.monotonic() - began
                            )
                        except RecognitionError as error:
                            self._line({"type": "error", "page": index + 1, "message": str(error)})
                            return
                        except Exception as error:  # noqa: BLE001 - reported to the caller
                            self._line(
                                {
                                    "type": "error",
                                    "page": index + 1,
                                    "message": f"Page {index + 1} could not be rendered: {error}",
                                }
                            )
                            return
                        self._line(
                            {"type": "page", "page": index + 1, "pages": pages, "musicxml": musicxml}
                        )
                self._line({"type": "done"})
            finally:
                _one_at_a_time.release()
        except (BrokenPipeError, ConnectionResetError):
            # The page that asked has gone; nothing is left to tell it. The
            # remaining pages are not read.
            self.close_connection = True
            return
        finally:
            if not self.close_connection:
                self._finish_chunks()

    def _line(self, message: dict) -> None:
        data = (json.dumps(message) + "\n").encode("utf-8")
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _finish_chunks(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        sys.stderr.write(f"[piano-omr] {self.address_string()} {format % args}\n")


def make_server(settings: Settings, recognise: Recogniser | None = None) -> ThreadingHTTPServer:
    handler = type(
        "BoundHandler",
        (Handler,),
        {"settings": settings, "recognise": staticmethod(recognise or homr_recogniser(settings))},
    )
    return ThreadingHTTPServer((settings.host, settings.port), handler)


def main() -> None:
    settings = Settings.from_env()
    if shutil.which(settings.homr) is None and not Path(settings.homr).exists():
        sys.exit(f"homr was not found at {settings.homr!r}. Install homr or set PIANO_OMR_HOMR.")
    if settings.host not in {"127.0.0.1", "localhost", "::1"} and not settings.source_url:
        sys.stderr.write(
            "[piano-omr] warning: listening beyond this machine with no PIANO_OMR_SOURCE_URL.\n"
            "            The AGPL asks that its users be offered the source; publish this\n"
            "            repository and set the variable before others use the service.\n"
        )
    server = make_server(settings)
    origins = ", ".join(sorted(settings.allowed_origins))
    sys.stderr.write(
        f"[piano-omr] homr {homr_version()} on http://{settings.host}:{settings.port} "
        f"(pages from: {origins})\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
