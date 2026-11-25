# piano-omr

Reads sheet music out of a PDF with [homr](https://github.com/liebharc/homr),
over HTTP, so the Piano Fingering app can open a scanned score on its own.

It is a separate program on purpose. homr — code and model weights — is
licensed AGPL-3.0, and so is this service. The app does no recognition itself;
it sends the PDF here and gets MusicXML back, and contains none of homr.

## Running it

With homr installed (Python 3.11 or 3.12):

```bash
pip install -e .
piano-omr
```

It listens on `http://127.0.0.1:8765` and accepts pages from
`http://localhost:5173`, the app's dev server. In the app, set
`VITE_OMR_URL=http://127.0.0.1:8765` in `.env.local`.

Or in Docker, which fetches homr's models (about 150 MB) at build time:

```bash
docker build -t piano-omr .
docker run -p 8765:8765 -e PIANO_OMR_ORIGINS=http://localhost:5173 piano-omr
```

| Variable | Default | |
|---|---|---|
| `PIANO_OMR_HOST` | `127.0.0.1` | `0.0.0.0` to listen beyond this machine |
| `PIANO_OMR_PORT` | `8765` | |
| `PIANO_OMR_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Pages that may call it, comma-separated |
| `PIANO_OMR_MAX_MB` | `25` | Largest PDF accepted |
| `PIANO_OMR_MAX_PAGES` | `20` | Most pages read in one request |
| `PIANO_OMR_PAGE_TIMEOUT` | `300` | Seconds homr may spend on one page |
| `PIANO_OMR_SOURCE_URL` | — | A public copy of this repository; see below |
| `PIANO_OMR_HOMR` | `homr` beside Python | The homr command |

## What it does

```
POST /v1/recognise      the PDF as the body, Content-Type: application/pdf
GET  /health            {"ok": true, "homr": "0.7.1"}
GET  /source            {"source": "<PIANO_OMR_SOURCE_URL>"}
```

`/v1/recognise` answers as it goes, one JSON object per line
(`application/x-ndjson`), so the app can show which page is being read:

```
{"type": "start", "pages": 2, "source": null}
{"type": "page", "page": 1, "pages": 2, "musicxml": "<?xml ..."}
{"type": "page", "page": 2, "pages": 2, "musicxml": "<?xml ..."}
{"type": "done"}
```

A page it cannot read ends the answer with
`{"type": "error", "page": 2, "message": "..."}`. A second request while one
is being read gets `{"type": "waiting"}` first and is read after it.

Each page is rendered here, whole, at 300 dpi, and given to homr as an image,
one page at a time. Handed a PDF, homr crops the page to the paper and writes
note positions in the cropped picture; handed several pages, it merges them
and loses the positions altogether. The app draws each fingering at its note's
position on the whole page, so neither will do.

## Before anyone else uses it

The AGPL asks that people who use a program over a network be offered its
source. Before this service is reachable by anyone but you:

1. Publish this repository (with any changes you have made).
2. Set `PIANO_OMR_SOURCE_URL` to it. The service returns it from `/source` and
   in every answer, and the app links to it next to the recognised score.

The service warns at startup when it listens beyond this machine without it.
Whether running homr as a separate service is enough to keep the app outside
the AGPL is homr's FAQ's view, not legal advice; see its
[licence FAQ](https://github.com/liebharc/homr/blob/main/LICENSE_FAQ.md).

The `Origin` check keeps other websites from using the service from a
visitor's browser. It is not access control: anything that is not a browser
can claim any origin. A public deployment wants a limit on who may call it.

## Tests

```bash
python -m unittest discover -s tests -t .
```

homr itself is replaced by a stand-in; the tests cover rendering, the order
and shape of the answer, the limits and the origin check.
