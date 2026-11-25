#!/usr/bin/env python3
"""Takes screenshot.png, the picture of the Archive tab that README.md shows.

    .venv/bin/python tools/screenshot.py

The page in the picture is the real one, served by osiris's own server over an empty archive in a
temporary folder. What makes it look like a well-used archive is screenshot.json beside this
script. Its folders and settings are saved the way the page saves them, and its numbers, folder
summaries and log are laid over what the server answers. So the picture shows the same archive on
any machine, and the page around it is whatever osiris/ui.html is now. Nothing is read from your
own archive or settings.

The picture is the whole Archive tab in a window 900 px wide, drawn 1676 pixels wide. GitHub's
README column is 838 px wide, so it shows that width pixel for pixel on a 2x screen and exactly
halved on a 1x one. The picture ends as far under the last line as the first card is under the
header. Text is antialiased in grey, because the coloured fringes a desktop browser gives text
turn into coloured speckle when GitHub draws the picture smaller than it is.

It needs Playwright's Chromium, as `osiris run --render` does, and the fonts screenshot.json
names. It stops without writing anything if the browser drew the page in other fonts or the page
didn't finish loading, and it says which of screenshot.json's names the server no longer knows.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from aiohttp import ClientSession, web  # noqa: E402

from osiris import ui  # noqa: E402

DATA = Path(__file__).with_suffix(".json")
PICTURE = REPO / "screenshot.png"
FAILURES_VERSION = 1
# the names a page's generic font families are asked of fontconfig by
GENERIC = {"sans-serif": ("sans-serif", "sans", "system-ui", "ui-sans-serif"), "monospace": ("monospace", "mono", "ui-monospace")}
# an element whose own text is in each generic family, to ask the browser which font it drew it with
SAMPLES = {"sans-serif": "#sources h2", "monospace": "#log"}
STILL_LOADING = ("Checking…", "Working it out…")


def warn(message: str, said: set[str] = set()) -> None:
    if message not in said:  # once each: the page asks for its state again every second
        said.add(message)
        print(f"screenshot: {message}", file=sys.stderr)


def stale(real: dict, shown: dict, where: str) -> None:
    """Say which of screenshot.json's names the server no longer answers with: the sign that it has fallen behind."""
    for key, value in shown.items():
        if key not in real:
            warn(f"{where}.{key} is in {DATA.name} but the server doesn't send it any more")
        elif isinstance(value, dict) and isinstance(real[key], dict) and real[key]:
            stale(real[key], value, f"{where}.{key}")


def overlay(data: dict):
    """A middleware that answers the page with the server's own JSON, with screenshot.json's values over it."""
    log = data["log"]

    def state(real: dict, request: web.Request) -> dict:
        shown = data["state"]
        stale({**real, "failures": {}}, shown, "state")
        since = int(request.query.get("since", 0))
        seen = request.query.get("failures") == str(FAILURES_VERSION)
        return {**real, **shown,
                "job": {**real["job"], **shown["job"], "count": len(log), "lines": log[since:]},
                "failures": None if seen else {**shown["failures"], "version": FAILURES_VERSION, "ready": True}}

    def config(real: dict, request: web.Request) -> dict:
        shown = data["config_shown"]
        stale(real, shown, "config_shown")
        return {**real, **shown, "defaults": {**real["defaults"], **shown["defaults"]}}

    def scan(real: dict, request: web.Request) -> dict:
        summary = data["scans"].get(request.query.get("kind"), {}).get(request.query.get("path"))
        return real if summary is None else {**real, "ok": True, "summary": summary}

    def cleanup(real: dict, request: web.Request) -> dict:
        stale(real, data["cleanup"], "cleanup")
        return {**real, **data["cleanup"]}

    shown = {"/api/state": state, "/api/config": config, "/api/scan": scan, "/api/cleanup": cleanup}

    @web.middleware
    async def middleware(request: web.Request, handler) -> web.StreamResponse:
        response = await handler(request)
        make = shown.get(request.path)
        if make is None or response.status != 200 or response.content_type != "application/json":
            return response
        return web.json_response(make(json.loads(response.text), request))

    return middleware


async def save_config(address: str, wanted: dict) -> None:
    """Save the folders and settings the way the page does, and say which the server didn't take."""
    async with ClientSession() as session, session.post(address + "api/config", json=wanted) as response:
        response.raise_for_status()
        saved = await response.json()
    for key, value in wanted.items():
        if key != "options" and saved.get(key) != value:
            warn(f"config.{key} in {DATA.name} wasn't taken by the server")
    for key, value in wanted["options"].items():
        if saved["options"].get(key) != value:
            warn(f"config.options.{key} in {DATA.name} wasn't taken by the server")


def fontconfig(fonts: dict[str, str], folder: str | None, lcd: bool, conf: Path) -> None:
    """Write a fontconfig file that is this machine's own, with the picture's fonts first for the generic families."""
    first = "".join(
        f'<match target="pattern"><test qual="any" name="family"><string>{asked}</string></test>'
        f'<edit name="family" mode="prepend" binding="strong"><string>{font}</string></edit></match>\n'
        for generic, font in fonts.items() for asked in GENERIC[generic])
    conf.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n'
        '<include ignore_missing="yes">/etc/fonts/fonts.conf</include>\n'
        + (f"<dir>{escape(str(Path(folder).resolve()))}</dir>\n" if folder else "") + first +
        '<match target="font"><edit name="antialias" mode="assign"><bool>true</bool></edit>'
        f'<edit name="rgba" mode="assign"><const>{"rgb" if lcd else "none"}</const></edit></match>\n</fontconfig>\n', encoding="utf-8")
    installed = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True,
                               env={**os.environ, "FONTCONFIG_FILE": str(conf)}).stdout
    families = {name.strip() for line in installed.splitlines() for name in line.split(",")}
    if missing := sorted(set(fonts.values()) - families):
        sys.exit(f"screenshot: the picture needs the font{'s' * (len(missing) > 1)} {' and '.join(missing)}, which "
                 "fontconfig doesn't have. Install them, or pass --fonts with a folder that holds their files.")


async def fonts_used(page, selector: str) -> set[str]:
    """The fonts the browser drew an element's own text with."""
    cdp = await page.context.new_cdp_session(page)
    try:
        await cdp.send("DOM.enable")
        await cdp.send("CSS.enable")
        root = (await cdp.send("DOM.getDocument"))["root"]["nodeId"]
        node = (await cdp.send("DOM.querySelector", {"nodeId": root, "selector": selector}))["nodeId"]
        return {font["familyName"] for font in (await cdp.send("CSS.getPlatformFontsForNode", {"nodeId": node}))["fonts"]}
    finally:
        await cdp.detach()


async def take(address: str, window: dict, scale: float, conf: Path, lcd: bool) -> bytes:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit("screenshot: this needs Playwright's Chromium: pip install -e '.[render]' && playwright install chromium")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, env={**os.environ, "FONTCONFIG_FILE": str(conf)},
            ignore_default_args=["--hide-scrollbars"],  # the log's scrollbar belongs in the picture
            args=["--enable-lcd-text" if lcd else "--disable-lcd-text"])
        try:
            width = window["width"]
            context = await browser.new_context(
                viewport={"width": width, "height": 1200}, device_scale_factor=scale, locale=window["locale"],
                timezone_id=window["timezone"], user_agent=window["user_agent"], color_scheme="light",
                is_mobile=True)  # which changes one thing here: the log's scrollbar is a thin bar with no track or arrows
            page = await context.new_page()
            await page.goto(address)
            await page.wait_for_function(
                """waiting => {
                    const text = document.querySelector("#archive .column")?.innerText ?? "";
                    return document.querySelector("#lanes table") && document.querySelector("#fail-table tr")
                        && document.querySelector("#log")?.textContent && document.querySelector("#cookies-note")?.textContent
                        && !waiting.some(words => text.includes(words));
                }""", arg=list(STILL_LOADING), timeout=30000)
            await page.wait_for_load_state("networkidle")
            for generic, selector in SAMPLES.items():
                used, wanted = await fonts_used(page, selector), window["fonts"][generic]
                if used != {wanted}:
                    sys.exit(f"screenshot: {selector} should be in {wanted} but the browser used {', '.join(sorted(used)) or 'nothing'}")
            # The window ends as far under the last line as the first card is under the header, which
            # is 48 px more than the content is high. The tab's own padding under it is deeper, so it is cut.
            measure = """() => {
                const column = document.querySelector("#archive .column"), pane = document.querySelector("#archive");
                return Math.ceil(column.lastElementChild.getBoundingClientRect().bottom + pane.scrollTop
                                 + parseFloat(getComputedStyle(column).paddingTop));
            }"""
            for _ in range(3):  # until the window's height no longer changes where the page ends
                height = await page.evaluate(measure)
                if page.viewport_size["height"] == height:
                    break
                await page.set_viewport_size({"width": width, "height": height})
            else:
                sys.exit("screenshot: the page's height wouldn't settle")
            # the log's scrollbar only shows for a moment after the log has moved, so move it to its end now
            await page.evaluate("""async () => {
                const log = document.querySelector("#log"), frame = () => new Promise(done => requestAnimationFrame(done));
                log.scrollTop = 0;
                await frame();
                log.scrollTop = log.scrollHeight;
                await frame();
                await frame();
            }""")
            return await page.screenshot(type="png", caret="hide")
        finally:
            await browser.close()


async def run(args: argparse.Namespace) -> None:
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    if args.page:
        ui.PAGE = Path(args.page).resolve()
    with tempfile.TemporaryDirectory(prefix="osiris-screenshot-") as tmp:
        conf = Path(tmp) / "fonts.conf"
        fontconfig(data["window"]["fonts"], args.fonts, args.lcd, conf)
        app = ui.make_app(Path(tmp) / "archive")
        app.middlewares.append(overlay(data))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            address = f"http://127.0.0.1:{runner.addresses[0][1]}/"
            await save_config(address, data["config"])
            window = data["window"]
            png = await take(address, window, (args.wide or window["picture_width"]) / window["width"], conf, args.lcd)
        finally:
            await runner.cleanup()
    from PIL import Image

    picture = Image.open(io.BytesIO(png)).convert("RGB")  # the page has no transparency to keep
    picture.save(args.out, optimize=True)
    print(f"screenshot: {args.out} is {picture.width} x {picture.height}, {os.path.getsize(args.out):,} bytes", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Take screenshot.png, the picture of the Archive tab in README.md.")
    parser.add_argument("--out", default=str(PICTURE), help="where to write the picture (default: screenshot.png in the repo)")
    parser.add_argument("--data", default=str(DATA), help="the archive to show (default: screenshot.json beside this script)")
    parser.add_argument("--page", help="another copy of ui.html to take the picture of, such as an older one to compare with")
    parser.add_argument("--wide", type=int, metavar="PIXELS",
                        help="how many pixels wide to make the picture (default: 1676, twice the 838 px of GitHub's "
                             "README column). The window stays 900 px wide, so the page looks the same")
    parser.add_argument("--fonts", metavar="FOLDER", help="a folder of font files to use as well as the installed ones")
    parser.add_argument("--lcd", action="store_true", help="antialias text for an LCD's coloured subpixels, as a desktop browser does")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
