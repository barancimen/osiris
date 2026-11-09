"""Numbers a run can change without touching the code: `osiris run --page-timeout 60`, or Advanced on the web page.

Each knob is a constant in the module that uses it, read at the moment it's needed, so setting the
constant is all it takes. Extraction processes get the same values when they start.
"""

from __future__ import annotations

import argparse
import importlib
from dataclasses import dataclass

# defaults of the `osiris run` flags that are plain arguments rather than knobs
FLAG_DEFAULTS = {"concurrency": 64, "per_host": 4, "worker_mem_mb": 1536, "render_concurrency": 2, "challenge_wait": 15.0}


@dataclass(frozen=True)
class Knob:
    flag: str
    module: str
    constant: str
    kind: type
    minimum: float
    text: str

    @property
    def option(self) -> str:
        """Its name in the web page's settings."""
        return self.flag.removeprefix("--").replace("-", "_")

    @property
    def default(self):
        return getattr(importlib.import_module(f"osiris.{self.module}"), self.constant)


KNOBS = (
    # when something fails
    Knob("--tries", "generic", "MAX_ATTEMPTS", int, 1, "tries per web page before it counts as failed"),
    Knob("--max-retry-wait", "generic", "MAX_COOLDOWN", float, 0, "longest wait, in seconds, before trying a page again"),
    Knob("--site-down-after", "generic", "HOST_DOWN_AFTER", int, 1,
         "connection failures in a row before the rest of a site is skipped"),
    Knob("--site-refused-after", "generic", "FORBIDDEN_AFTER", int, 1,
         "refusals (403, or 429 after its retries) in a row before the rest of a site is skipped"),
    Knob("--youtube-block-wait", "youtube", "BLOCK_WAIT_MINUTES", float, 0,
         "minutes to pause after YouTube blocks you; doubles with each block in a row"),
    Knob("--youtube-block-limit", "youtube", "BLOCK_LIMIT", int, 1,
         "blocks in a row before the YouTube lane stops until the next run"),
    # waiting between requests
    Knob("--arxiv-delay", "arxiv", "FULLTEXT_DELAY", float, 0, "seconds between arXiv full-text requests; arXiv asks for 15"),
    Knob("--wayback-delay", "generic", "WAYBACK_INTERVAL", float, 0, "seconds between Wayback Machine requests"),
    Knob("--stackexchange-delay", "stackexchange", "MIN_DELAY", float, 0, "seconds between Stack Exchange requests"),
    # time limits
    Knob("--page-timeout", "generic", "PAGE_TIMEOUT", float, 1, "seconds to download one page or PDF"),
    Knob("--stall-timeout", "generic", "STALL_TIMEOUT", float, 1, "seconds a download may send nothing before it's dropped"),
    Knob("--read-timeout", "extract", "TASK_TIMEOUT", int, 1, "seconds to get the text out of one page or PDF"),
    Knob("--book-timeout", "documents", "TIMEOUT", int, 1, "seconds to read one ebook or Word document"),
    Knob("--ocr-page-timeout", "ocr", "PAGE_TIMEOUT", int, 1, "seconds for OCR on one page"),
    Knob("--ocr-document-timeout", "ocr", "DOC_TIMEOUT", int, 1, "seconds for OCR on one document; what's read by then is kept"),
    # what counts as good text
    Knob("--ocr-dpi", "ocr", "DPI", int, 72, "how finely OCR renders each page; higher is slower and sometimes better"),
    Knob("--thin-under", "extract", "MIN_CHARS", int, 0, "a document with fewer characters than this counts as thin"),
)

_chosen: dict[str, float] = {}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    for knob in KNOBS:
        parser.add_argument(knob.flag, type=knob.kind, metavar="N", help=f"{knob.text} (default {knob.default:g})")


def set_constants(values: dict[str, float]) -> None:
    """{knob flag: value}. Also what a new extraction process calls with chosen()."""
    for knob in KNOBS:
        if knob.flag in values:
            setattr(importlib.import_module(f"osiris.{knob.module}"), knob.constant, values[knob.flag])
            _chosen[knob.flag] = values[knob.flag]


def apply_arguments(args: argparse.Namespace) -> None:
    given = {knob.flag: getattr(args, knob.option) for knob in KNOBS if getattr(args, knob.option) is not None}
    for knob in KNOBS:
        if knob.flag in given and given[knob.flag] < knob.minimum:
            raise SystemExit(f"{knob.flag} should be at least {knob.minimum:g}")
    set_constants(given)


def chosen() -> dict[str, float]:
    return dict(_chosen)
