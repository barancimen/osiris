"""Sessions with coding agents, from the logs they keep on your computer:
- Claude Code: ~/.claude/projects/<folder>/<session>.jsonl, for each profile, and the prompts you typed,
  in ~/.claude/history.jsonl
- pi: ~/.pi/agent/sessions/<folder>/<session>.jsonl
- omp: ~/.omp/agent/sessions/<folder>/<session>.jsonl, and omp-serve's sessions at the top of that folder

Each session becomes one document, keyed by the session's ID, which chats.Saver rewrites when the session
has grown. A document is the conversation between you and the model on the branch you were last on: your
prompts and what you pasted, prompts from guests in an omp-serve session, the text the model wrote to you,
the plans it showed you for approval, and the questions it asked you with your answers. Reasoning, tool
calls and their results, subagents, the text a harness adds to your messages, and compaction summaries
are left out, and so is a one-line note the model writes just before a tool call. Headings inside messages
are made third-level, so that the second-level headings say who is speaking and nothing else.

Claude Code deletes a session's log after 30 days, but keeps your prompts in history.jsonl. A session
whose log isn't in the archive gets a document of your prompts alone, which the log replaces if it turns
up. A session holding another one whole, as a resumed copy can, takes over the shorter one's document.

pi and omp write the same format, so a file is taken to be omp's when it sits under a folder named omp or
.omp, pi's under pi or .pi, and otherwise omp's only if it has entries pi doesn't write.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path

from .chats import Chat, Turn, _time

log = logging.getLogger(__name__)

READER = 1  # raise it when session documents should come out differently
SUFFIX = ".jsonl"
HEAD = 64  # lines read to tell what a file is
PASTE_WORDS = 2000  # a longer paste or attachment becomes one line saying what it was
NOTE_CHARS = 300  # a note before a tool call, on one line and no longer than this, is left out
LOG_NAME = re.compile(r"\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-\d+Z_[0-9a-f-]{36}")  # how pi and omp name a session's log
OMP_ONLY = {"title", "title_change", "session_init", "mode_change", "service_tier_change", "model_usage", "call"}

REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
PASTE = re.compile(r"<(pasted_content|attachment)\b[^>]*>(.*?)</\1\b[^>]*>", re.S)
PASTE_MARK = re.compile(r"\[Pasted text #(\d+)(?: \+\d+ lines)?\]")
HARNESS_TEXT = re.compile(r"<(local-command-stdout|local-command-stderr|local-command-caveat|bash-input|bash-stdout"
                          r"|bash-stderr|task-notification|user-prompt-submit-hook)\b"
                          r"|\[Request interrupted by user|Caveat: The messages below were generated")
FENCE = re.compile(r"\s{0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"#{1,2}(?=\s|$)")
REJECTED = "The user doesn't want to proceed with this tool use."
SAID = "the user said:\n"
CLARIFY = "The user wants to clarify these questions"


def side_file(path: Path) -> bool:
    """A subagent's log, or an omp advisor's, which repeats the session it belongs to: in Claude Code's
    subagents folder, or in the folder omp keeps beside a session's log, named as the log is."""
    folder = path.parent.name
    return folder == "subagents" or bool(LOG_NAME.fullmatch(folder)) or (path.parent.parent / f"{folder}{SUFFIX}").is_file()


def _lines(path: Path) -> Iterator[dict]:
    """Each entry of a log. A line that isn't JSON, such as the last one of a session still being
    written, is passed over."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry


def _harness(path: Path) -> str | None:
    for part in reversed(path.parts[:-1]):
        if part.lstrip(".") in ("omp", "pi"):
            return part.lstrip(".")
    return None


def kind(path: Path) -> str | None:
    """'claude-code', 'history' (Claude Code's prompts), 'omp' or 'pi', from the first lines; None for
    any other file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(HEAD), f):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if {"display", "timestamp", "project"} <= entry.keys():
                    return "history"
                if "sessionId" in entry and "type" in entry:
                    return "claude-code"
                if entry.get("type") == "title" and "v" in entry:
                    return "omp"
                if entry.get("type") == "session" and "version" in entry and "id" in entry:
                    return _harness(path) or "pi"
    except OSError:
        return None
    return None


# -- text -----------------------------------------------------------------------------------------

def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().split("\n"))


def _long_paste(text: str) -> str:
    words = text.split()
    if len(words) <= PASTE_WORDS:
        return text
    return f"*Pasted text of {len(words):,} words, left out. It began: {' '.join(words[:12])}…*"


def _pastes(text: str) -> str:
    """Text with what you pasted or attached in it as its own words, or as a line if it's very long."""
    return PASTE.sub(lambda m: _long_paste(m.group(2).strip()), text)


def _demote(text: str) -> str:
    """First- and second-level headings outside code blocks made third-level."""
    lines, fence = [], None
    for line in text.split("\n"):
        if m := FENCE.match(line):
            mark = m.group(1)
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence) and not line.strip()[len(mark):]:
                fence = None
        elif fence is None and (m := HEADING.match(line)):
            line = "###" + line[m.end():]
        lines.append(line)
    return "\n".join(lines)


def _typed(text: str) -> str:
    """What you typed into Claude Code, without what the harness put around it or in its place. A slash
    command stays when you gave it more than one word."""
    text = REMINDER.sub("", text).strip()
    if text.startswith("<command-"):
        name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        args = args.group(1).strip() if args else ""
        return f"{name.group(1).strip()} {args}" if name and len(args.split()) > 1 else ""
    if HARNESS_TEXT.match(text):
        return ""
    return _pastes(text)


def _attached(images: int, documents: int = 0) -> str:
    def count(n: int, what: str) -> str:
        return f"{n} {what}s" if n > 1 else f"{'an' if what[0] in 'aeiou' else 'a'} {what}"
    names = [count(n, what) for n, what in ((images, "image"), (documents, "document")) if n]
    return f"*Attached {' and '.join(names)}*" if names else ""


def _answers(pairs: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"{_quote(q)}\n\n{a}" for q, a in pairs if q and a)


# -- turns ----------------------------------------------------------------------------------------
# A log's entries become items: ("you", when, text, name), ("model", when, text, model), ("tool",) for a
# tool call, and in omp ("draft", text) for a plan written and ("shown", when, model) for it shown to you.

def _turns(items: list[tuple]) -> list[Turn]:
    turns, draft = [], None
    for i, item in enumerate(items):
        if item[0] == "draft":
            draft = item[1]
        elif item[0] == "shown":
            if draft:
                turns.append(Turn(False, item[1], ["### Plan\n\n" + _demote(draft)], item[2]))
            draft = None
        elif item[0] in ("you", "model"):
            kind, when, text, extra = item
            text = text.strip()
            if not text:
                continue
            if kind == "model" and "\n" not in text and len(text) <= NOTE_CHARS \
                    and i + 1 < len(items) and items[i + 1][0] == "tool":
                continue
            you = kind == "you"
            turns.append(Turn(you, when, [_demote(text)], None if you else extra, name=extra if you else None))
    return turns


# -- Claude Code ----------------------------------------------------------------------------------

def _claude_items(entry: dict, asked: set, unknown: Counter) -> list[tuple]:
    kind, when = entry.get("type"), _time(entry.get("timestamp"))
    m = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    content = m.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else \
        [b for b in content or [] if isinstance(b, dict)]
    items: list[tuple] = []
    if kind == "assistant":
        model = m.get("model") if isinstance(m.get("model"), str) else None
        if model == "<synthetic>":  # an error or a canned reply from Claude Code itself
            return []
        for b in blocks:
            if b.get("type") == "text":
                items.append(("model", when, b.get("text") or "", model))
            elif b.get("type") == "tool_use":
                items.append(("tool",))
                args = b.get("input") if isinstance(b.get("input"), dict) else {}
                if b.get("name") == "ExitPlanMode" and isinstance(args.get("plan"), str):
                    items.append(("model", when, "### Plan\n\n" + args["plan"], model))
                elif b.get("name") == "AskUserQuestion":
                    asked.add(b.get("id"))
        return items
    if kind == "attachment":
        a = entry.get("attachment") if isinstance(entry.get("attachment"), dict) else {}
        if a.get("type") == "queued_command" and isinstance(a.get("prompt"), str):  # typed while the model was busy
            items.append(("you", when, _typed(a["prompt"]), None))
        return items
    if kind != "user" or entry.get("isMeta") or entry.get("isCompactSummary"):
        return items
    texts, images, documents = [], 0, 0
    for b in blocks:
        btype = b.get("type")
        if btype == "text":
            texts.append(_typed(b.get("text") or ""))
        elif btype == "image":
            images += 1
        elif btype == "document":
            documents += 1
        elif btype == "tool_result":
            result = entry.get("toolUseResult")
            if b.get("tool_use_id") in asked and isinstance(result, dict) and isinstance(result.get("answers"), dict):
                texts.append(_answers([(str(q), str(a)) for q, a in result["answers"].items()]))
                continue
            said = b.get("content")
            said = said if isinstance(said, str) else "\n".join(
                x.get("text") or "" for x in said or [] if isinstance(x, dict) and x.get("type") == "text")
            if said.startswith(REJECTED) and SAID in said:  # you turned a tool call down and said why
                said = said.split(SAID, 1)[1].strip()
                if not said.startswith(CLARIFY):
                    texts.append(said)
        else:
            unknown[f"Claude Code's {btype}"] += 1
    texts.append(_attached(images, documents))
    text = "\n\n".join(t for t in texts if t)
    return [("you", when, text, None)] if text else []


def _claude_code(path: Path, unknown: Counter) -> Chat | None:
    nodes: dict[str, tuple] = {}  # uuid: (parent, items, whether it's a message), for the main conversation
    last, sid, folder, created, newest, messages = None, None, None, None, "", 0
    titles: dict[str, str] = {}
    asked: set = set()
    for entry in _lines(path):
        kind = entry.get("type")
        for field, name in (("customTitle", "custom"), ("aiTitle", "ai"), ("summary", "summary")):
            if isinstance(entry.get(field), str) and entry[field].strip() and kind in ("custom-title", "ai-title", "summary"):
                titles[name] = entry[field].strip()
        uid = entry.get("uuid")
        if not isinstance(uid, str) or entry.get("isSidechain"):
            continue
        is_message = kind in ("user", "assistant")
        nodes[uid] = (entry.get("parentUuid") or entry.get("logicalParentUuid"), _claude_items(entry, asked, unknown), is_message)
        last = uid
        sid, folder = sid or entry.get("sessionId"), folder or entry.get("cwd")
        if is_message:
            messages += 1
            when = _time(entry.get("timestamp")) or ""
            created, newest = created or when or None, max(newest, when)
    if last is None:
        return None
    sid = path.stem if re.fullmatch(r"[0-9a-f-]{36}", path.stem) else sid
    if not isinstance(sid, str):
        return None
    keys = _branch_keys(nodes, last)
    branch = [nodes[k] for k in keys]
    ids = tuple(f"claude-code:{k}" for k in keys if nodes[k][2])
    return Chat("claude-code", sid, titles.get("custom") or titles.get("ai") or titles.get("summary") or "", created,
                None, newest, messages, _turns([i for node in branch for i in node[1]]), "chat:claude-code",
                rank=1, folder=folder if isinstance(folder, str) else None, ids=ids, reader=READER)


def _branch_keys(nodes: dict, leaf: str) -> list[str]:
    """The keys of the entries from the first one to `leaf`, each found as the one before's parent."""
    keys, seen = [], set()
    while leaf in nodes and leaf not in seen:
        seen.add(leaf)
        keys.append(leaf)
        leaf = nodes[leaf][0]
    return keys[::-1]


# -- pi and omp -----------------------------------------------------------------------------------

def _pi_items(entry: dict, asked: set, unknown: Counter, harness: str) -> list[tuple]:
    kind, when = entry.get("type"), _time(entry.get("timestamp"))
    if kind == "custom_message" and entry.get("customType") == "collab-prompt":  # a guest in omp-serve
        text = str(entry.get("content") or "").strip()
        return [] if text.startswith("!") else [("you", when, _pastes(text), "Guest")]
    if kind != "message" or not isinstance(entry.get("message"), dict):
        return []
    m = entry["message"]
    role, content = m.get("role"), m.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else \
        [b for b in content or [] if isinstance(b, dict)]
    if role == "user":
        texts, images = [], 0
        for b in blocks:
            if b.get("type") == "text":
                texts.append(_pastes(REMINDER.sub("", b.get("text") or "")).strip())
            elif b.get("type") == "image":
                images += 1
            else:
                unknown[f"{harness}'s {b.get('type')}"] += 1
        texts.append(_attached(images))
        return [("you", when, "\n\n".join(t for t in texts if t), None)]
    if role == "assistant":
        items: list[tuple] = []
        model = m.get("model") if isinstance(m.get("model"), str) else None
        for b in blocks:
            if b.get("type") == "text":
                items.append(("model", when, b.get("text") or "", model))
            elif b.get("type") == "toolCall":
                items.append(("tool",))
                name, args = b.get("name"), b.get("arguments") if isinstance(b.get("arguments"), dict) else {}
                path = args.get("path")
                if name == "write" and isinstance(path, str) and path.startswith("local://") and path.endswith("plan.md") \
                        and isinstance(args.get("content"), str):
                    items.append(("draft", args["content"]))
                elif name == "resolve" and args.get("action") == "apply":
                    items.append(("shown", when, model))
                elif name == "ask":
                    asked.add(b.get("id"))
        return items
    if role == "toolResult" and m.get("toolCallId") in asked and isinstance(m.get("details"), dict):
        details = m["details"]
        pairs = []
        for r in details.get("results") if isinstance(details.get("results"), list) else [details]:
            if isinstance(r, dict):
                chosen = [str(x) for x in r.get("selectedOptions") or []] + ([str(r["customInput"])] if r.get("customInput") else [])
                pairs.append((str(r.get("question") or ""), ", ".join(chosen)))
        return [("you", when, _answers(pairs), None)]
    return []  # tool results, shell commands you ran, and omp's notes to the model


def _pi(path: Path, unknown: Counter, harness: str | None) -> Chat | None:
    header: dict = {}
    nodes: dict[str, tuple] = {}  # id: (parent, items, whether it's a message)
    order: list[str] = []  # entries with an ID, as written
    titles, times, said, omp_only = [], [], 0, False
    asked: set = set()
    for entry in _lines(path):
        kind = entry.get("type")
        if kind == "session":
            header = header or entry
        if kind in ("session", "title", "title_change") and str(entry.get("title") or "").strip():
            titles.append(str(entry["title"]).strip())
        if kind == "session":
            continue
        omp_only = omp_only or kind in OMP_ONLY or entry.get("customType") == "tool_execution_start"
        if kind == "message" or entry.get("customType") == "collab-prompt":
            said += 1
            if when := _time(entry.get("timestamp")):
                times.append(when)
        key = entry.get("id") if isinstance(entry.get("id"), str) else f"#{len(nodes)}"  # older sessions have no IDs
        nodes[key] = (entry.get("parentId"), _pi_items(entry, asked, unknown, harness or "pi or omp"), kind == "message")
        if isinstance(entry.get("id"), str):
            order.append(key)
    sid = header.get("id")
    if not isinstance(sid, str):
        return None
    harness = harness or ("omp" if omp_only else "pi")
    keys = _branch_keys(nodes, order[-1]) if order else list(nodes)
    ids = tuple(f"{harness}:{k}" for k in keys if nodes[k][2] and not k.startswith("#"))
    folder = header.get("cwd")
    return Chat(harness, sid, titles[-1] if titles else "", _time(header.get("timestamp")) or min(times, default=None),
                None, max(times, default=""), said, _turns([i for k in keys for i in nodes[k][1]]), f"chat:{harness}",
                rank=1, folder=folder if isinstance(folder, str) else None, ids=ids, reader=READER)


# -- Claude Code's prompt history -----------------------------------------------------------------

def _prompt(entry: dict) -> str:
    text = entry.get("display")
    if not isinstance(text, str) or text.startswith("!") or (text.startswith("/") and len(text.split()) < 3):
        return ""  # a shell command, or a slash command given at most one word
    pasted = entry.get("pastedContents") if isinstance(entry.get("pastedContents"), dict) else {}

    def paste(m: re.Match) -> str:
        p = pasted.get(m.group(1))
        return _long_paste(p["content"]) if isinstance(p, dict) and isinstance(p.get("content"), str) else m.group(0)
    return PASTE_MARK.sub(paste, text)


def _history(path: Path) -> Iterator[Chat]:
    sessions: dict[str, list[dict]] = defaultdict(list)
    for entry in _lines(path):
        if isinstance(entry.get("sessionId"), str):
            sessions[entry["sessionId"]].append(entry)
    for sid, entries in sessions.items():
        times = [_time(e["timestamp"] / 1000 if isinstance(e.get("timestamp"), (int, float)) else e.get("timestamp"))
                 for e in entries]
        turns = [Turn(True, when, [_demote(text)]) for e, when in zip(entries, times) if (text := _prompt(e).strip())]
        known = [t for t in times if t]
        project = entries[0].get("project")
        yield Chat("claude-code", sid, "", min(known, default=None), None, max(known, default=""), len(entries), turns,
                   "chat:claude-code:history", rank=0, folder=project if isinstance(project, str) else None,
                   reader=READER, merge=False)


def read(path: Path, unknown: Counter) -> Iterator[Chat | None]:
    """The session in a log, or each session in Claude Code's prompt history, or None for a session that
    couldn't be read. Nothing for any other file."""
    what = kind(path)
    try:
        if what == "history":
            yield from _history(path)
        elif what in ("claude-code", "omp", "pi"):
            chat = _claude_code(path, unknown) if what == "claude-code" else \
                _pi(path, unknown, _harness(path) or (what if what == "omp" else None))
            if chat:  # None for a log with no conversation in it
                yield chat
    except (TypeError, AttributeError, KeyError, IndexError, ValueError) as e:  # a change in the format
        log.warning("%s: couldn't read the session: %s: %s", path, type(e).__name__, e)
        yield None
