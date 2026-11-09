"""Collecting saved URLs from backups: the same page once, and each exact URL only worked out once."""

import json
import sys
from pathlib import Path

import extract_urls


def backup(folder: Path, name: str, urls: list[str]) -> None:
    links = [{"url": u} for u in urls]
    (folder / name).write_text(json.dumps({"collections": [{"folders": [{"links": links}]}], "history": []}))


def collect(tmp_path: Path, monkeypatch) -> list[str]:
    out = tmp_path / "urls.txt"
    monkeypatch.setattr(sys, "argv", ["extract_urls.py", "--input-dir", str(tmp_path / "in"), "--output", str(out)])
    extract_urls.main()
    return out.read_text().splitlines()


def test_repeats_are_dropped_keeping_the_first_spelling_in_order(tmp_path: Path, monkeypatch, capsys):
    (tmp_path / "in").mkdir()
    backup(tmp_path / "in", "session-buddy-1.json", ["https://b.example/?utm_source=x", "chrome://settings", "https://a.example/"])
    backup(tmp_path / "in", "session-buddy-2.json", ["https://a.example/", "https://b.example/", "chrome://settings", "https://c.example/"])
    assert collect(tmp_path, monkeypatch) == ["https://b.example/?utm_source=x", "https://a.example/", "https://c.example/"]
    report = capsys.readouterr().err
    assert "session-buddy-1.json: 3 total, 1 filtered, 2 new, running unique 2" in report
    assert "session-buddy-2.json: 4 total, 1 filtered, 1 new, running unique 3" in report


def test_each_exact_url_is_only_worked_out_once(tmp_path: Path, monkeypatch):
    (tmp_path / "in").mkdir()
    for n in range(3):
        backup(tmp_path / "in", f"session-buddy-{n}.json", ["https://a.example/", "https://b.example/", "chrome://settings"])
    filtered, keyed = [], []
    real_keep, real_key = extract_urls.keep, extract_urls.dedup_key
    monkeypatch.setattr(extract_urls, "keep", lambda u: filtered.append(u) or real_keep(u))
    monkeypatch.setattr(extract_urls, "dedup_key", lambda u: keyed.append(u) or real_key(u))
    assert len(collect(tmp_path, monkeypatch)) == 2
    assert sorted(filtered) == ["chrome://settings", "https://a.example/", "https://b.example/"]
    assert sorted(keyed) == ["https://a.example/", "https://b.example/"]
