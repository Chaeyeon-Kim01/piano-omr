"""The service's own behaviour, with homr replaced by a stand-in.

homr itself takes a quarter of a minute a page and a gigabyte of models, so
it is not run here; what is tested is everything around it - which pages are
rendered and how, what is sent back and in what order, and what is refused.
"""

from __future__ import annotations

import http.client
import io
import json
import threading
import unittest
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from piano_omr.server import RecognitionError, Settings, make_server


def pdf_with_pages(*sizes: tuple[float, float]) -> bytes:
    document = pdfium.PdfDocument.new()
    for width, height in sizes:
        document.new_page(width, height)
    out = io.BytesIO()
    document.save(out)
    document.close()
    return out.getvalue()


LETTER = (612.0, 792.0)


class Running:
    """A server on a free port for the length of a test."""

    def __init__(self, recognise, **settings):
        self.calls: list[tuple[str, tuple[int, int], bool]] = []

        def recorded(image: Path, with_title: bool) -> str:
            with Image.open(image) as picture:
                self.calls.append((image.name, picture.size, with_title))
            return recognise(image, with_title)

        self.server = make_server(Settings(port=0, **settings), recorded)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def recognise(self, pdf: bytes, origin: str | None = None):
        headers = {"Content-Type": "application/pdf"}
        if origin:
            headers["Origin"] = origin
        response, data = self.request("POST", "/v1/recognise", pdf, headers)
        lines = [json.loads(line) for line in data.decode().splitlines() if line.strip()]
        return response, lines


def fake(image: Path, with_title: bool) -> str:
    return f"<score-partwise><!-- {image.name} --></score-partwise>"


class Recognition(unittest.TestCase):
    def test_answers_a_page_at_a_time_in_order(self):
        with Running(fake) as server:
            response, lines = server.recognise(pdf_with_pages(LETTER, LETTER, LETTER))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/x-ndjson")
        self.assertEqual([line["type"] for line in lines], ["start", "page", "page", "page", "done"])
        self.assertEqual(lines[0]["pages"], 3)
        self.assertEqual([line["page"] for line in lines[1:4]], [1, 2, 3])
        self.assertIn("page-002.png", lines[2]["musicxml"])

    def test_renders_each_page_whole_at_300_dpi(self):
        # The app places homr's positions on the whole page at 300 dpi; a
        # cropped or rescaled picture would move every number.
        with Running(fake) as server:
            server.recognise(pdf_with_pages(LETTER, (595.0, 842.0)))
        # pdfium can round a side up by a pixel - letter paper comes out
        # 2550 x 3301 - so a pixel either way is allowed. The app works from
        # the width, which it computes the same way.
        width, height = server.calls[0][1]
        self.assertAlmostEqual(width, 2550, delta=1)
        self.assertAlmostEqual(height, 3300, delta=1)
        width, height = server.calls[1][1]
        self.assertAlmostEqual(width, 595 * 300 / 72, delta=1)
        self.assertAlmostEqual(height, 842 * 300 / 72, delta=1)

    def test_reads_the_title_on_the_first_page_only(self):
        with Running(fake) as server:
            server.recognise(pdf_with_pages(LETTER, LETTER))
        self.assertEqual([call[2] for call in server.calls], [True, False])

    def test_stops_at_a_page_it_cannot_read_and_says_which(self):
        def fails_on_second(image: Path, with_title: bool) -> str:
            if image.name == "page-002.png":
                raise RecognitionError("No staff found.")
            return fake(image, with_title)

        with Running(fails_on_second) as server:
            _, lines = server.recognise(pdf_with_pages(LETTER, LETTER, LETTER))
        self.assertEqual([line["type"] for line in lines], ["start", "page", "error"])
        self.assertEqual(lines[-1]["page"], 2)
        self.assertEqual(lines[-1]["message"], "No staff found.")
        self.assertEqual(len(server.calls), 2)


class Refusals(unittest.TestCase):
    def test_refuses_what_is_not_a_pdf(self):
        with Running(fake) as server:
            response, _ = server.request(
                "POST", "/v1/recognise", b"<score-partwise/>", {"Content-Type": "application/pdf"}
            )
        self.assertEqual(response.status, 415)

    def test_refuses_more_pages_than_it_reads_at_once(self):
        with Running(fake, max_pages=2) as server:
            response, data = server.request(
                "POST", "/v1/recognise", pdf_with_pages(LETTER, LETTER, LETTER)
            )
        self.assertEqual(response.status, 422)
        self.assertIn("3 pages", json.loads(data)["error"])
        self.assertEqual(server.calls, [])

    def test_refuses_a_file_over_the_size_limit_without_reading_it(self):
        with Running(fake, max_bytes=100) as server:
            response, _ = server.request("POST", "/v1/recognise", b"%PDF" + b"0" * 200)
        self.assertEqual(response.status, 413)


class Origins(unittest.TestCase):
    def test_answers_the_app_with_its_origin(self):
        with Running(fake) as server:
            response, _ = server.recognise(pdf_with_pages(LETTER), origin="http://localhost:5173")
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "http://localhost:5173")

    def test_turns_away_other_websites(self):
        with Running(fake) as server:
            response, _ = server.request(
                "POST", "/v1/recognise", pdf_with_pages(LETTER), {"Origin": "https://elsewhere.example"}
            )
        self.assertEqual(response.status, 403)
        self.assertEqual(server.calls, [])

    def test_lets_the_browser_ask_before_sending_a_pdf(self):
        # application/pdf is not a type a browser sends cross-origin without
        # asking first.
        with Running(fake) as server:
            response, _ = server.request(
                "OPTIONS",
                "/v1/recognise",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
        self.assertEqual(response.status, 204)
        self.assertIn("POST", response.getheader("Access-Control-Allow-Methods"))
        self.assertIn("Content-Type", response.getheader("Access-Control-Allow-Headers"))


class Health(unittest.TestCase):
    def test_says_it_is_up_and_which_homr(self):
        with Running(fake) as server:
            response, data = server.request("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(data)["ok"])

    def test_points_to_its_source_when_given_one(self):
        with Running(fake, source_url="https://example.org/piano-omr") as server:
            _, data = server.request("GET", "/source")
            _, lines = server.recognise(pdf_with_pages(LETTER))
        self.assertEqual(json.loads(data)["source"], "https://example.org/piano-omr")
        self.assertEqual(lines[0]["source"], "https://example.org/piano-omr")


class SettingsFromEnvironment(unittest.TestCase):
    def test_reads_origins_and_limits(self):
        settings = Settings.from_env(
            {
                "PIANO_OMR_ORIGINS": "https://piano.example/, http://localhost:5173",
                "PIANO_OMR_MAX_MB": "10",
                "PIANO_OMR_MAX_PAGES": "5",
            }
        )
        self.assertEqual(
            settings.allowed_origins, {"https://piano.example", "http://localhost:5173"}
        )
        self.assertEqual(settings.max_bytes, 10 * 1024 * 1024)
        self.assertEqual(settings.max_pages, 5)


if __name__ == "__main__":
    unittest.main()
