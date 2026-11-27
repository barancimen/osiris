"""Conversations with AI chatbots, from the files their sites and a browser extension give you:
- Claude's data export: conversations.json, inside the conversations-NNN.zip parts its manifest lists,
  or inside the single zip older exports came as
- Conversations saved as JSON with the Claude Conversation Exporter extension
  (github.com/socketteer/Claude-Conversation-Exporter), one per file or zipped together
- ChatGPT's data export: conversations.json, or conversations-NNN.json in newer exports, loose or zipped
- DeepSeek's data export: one JSON file per conversation, in chat_sessions/

Each conversation becomes one document, keyed by the conversation's own ID, so its copies from several
exports, or from an export and the extension, are one document. The newest copy is kept, whatever order
the files are read in: the one with the newest message, then the latest updated time, then the one from
the extension, which knows the branch you were on. A copy with fewer messages than the one saved never
replaces it, and a conversation deleted from the archive stays deleted.

A document is the branch you were on, as the chat showed it: your messages and the replies, the files and
artifacts the assistant wrote, its web searches and the pages it read as a line each, and the addresses
it cited. Thinking, tool results, images and text the site adds to a conversation are left out.

Files are read as a stream, a conversation at a time. A file read through before is skipped unless it
has changed, or READER has. Sessions with coding agents (Claude Code, pi and omp) come from their .jsonl
logs, which sessions.py reads, and are saved here the same way.
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

from . import extract
from .route import chat_key, doc_id, route
from .singlefile import file_sha1
from .store import Store, now_iso
from .tweetdb import READ_ERRORS, _Json

log = logging.getLogger(__name__)

READER = 1  # raise it when documents should come out differently: every file is then read and written again
LANE = "chat"
SUFFIXES = {".json", ".zip", ".jsonl"}
OBJECT_LIMIT = 64 << 20  # bytes: a file holding one conversation is read whole, so a bigger one is passed over
TITLE_CHARS = 80  # of the first message, for a conversation with no title
PROVIDERS = {  # name in the doc key: the assistant's name, and the conversation's address
    "claude": ("Claude", "https://claude.ai/chat/{}"),
    "chatgpt": ("ChatGPT", "https://chatgpt.com/c/{}"),
    "deepseek": ("DeepSeek", "https://chat.deepseek.com/a/chat/s/{}"),
    # sessions have no web address, so theirs is one that nothing fetches
    "claude-code": ("Claude Code", "session://claude-code/{}"),
    "omp": ("omp", "session://omp/{}"),
    "pi": ("pi", "session://pi/{}"),
}
SURVEY = {  # (provider, one conversation per file): what the page calls such a file, one and several
    ("claude", False): ("Claude export file", "Claude export files"),
    ("claude", True): ("Claude chat from the extension", "Claude chats from the extension"),
    ("chatgpt", False): ("ChatGPT export file", "ChatGPT export files"),
    ("chatgpt", True): ("ChatGPT chat", "ChatGPT chats"),
    ("deepseek", False): ("DeepSeek export file", "DeepSeek export files"),
    ("deepseek", True): ("DeepSeek chat", "DeepSeek chats"),
    ("claude-code", True): ("Claude Code session", "Claude Code sessions"),
    ("claude-code", False): ("Claude Code prompt history", "Claude Code prompt histories"),
    ("omp", True): ("omp session", "omp sessions"),
    ("pi", True): ("pi session", "pi sessions"),
}
TIME = re.compile(r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(?:\.(\d+))?\s*(Z|[+-]\d\d:?\d\d)?", re.I)


@dataclass
class Turn:
    you: bool
    when: str | None
    parts: list[str]  # markdown, in the order the chat showed it
    model: str | None = None
    name: str | None = None  # who wrote it, when it's neither you nor the assistant: a guest in an omp-serve session


@dataclass
class Chat:
    provider: str
    id: str
    title: str
    created: str | None
    updated: str | None
    newest: str  # the newest message in any branch
    messages: int  # in every branch
    turns: list[Turn]  # the branch you were on
    source: str
    rank: int = 0  # 1 for a copy that knows the branch you were on, which the extension's do
    model: str | None = None
    folder: str | None = None  # where a session with a coding agent ran
    ids: tuple = ()  # a session's messages on its branch, first to last, to tell a copy of it
    reader: int = READER  # the version of the code that read it
    merge: bool = True  # whether messages in a row from the same side are one turn

    @property
    def key(self) -> str:
        return chat_key(self.provider, self.id)

    @property
    def url(self) -> str:
        return PROVIDERS[self.provider][1].format(self.id)

    @property
    def fresh(self) -> tuple:
        return self.newest, self.updated or "", self.messages, self.rank


def _time(value) -> str | None:
    """A time as any of the exports give it, as 2026-11-14T21:00:35.162849Z in UTC, so that times compare as text."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            moment = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str) and (m := TIME.fullmatch(value.strip())):
            date, clock, fraction, zone = m.groups()
            zone = "+00:00" if not zone or zone.upper() == "Z" else f"{zone[:3]}:{zone[-2:]}"
            moment = datetime.fromisoformat(f"{date}T{clock}.{(fraction or '0')[:6].ljust(6, '0')}{zone}")
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _shown(time: str | None, seconds: bool = False) -> str | None:
    return f"{time[:10]} {time[11:19 if seconds else 16]}" if time else None


def _fence(text: str, language: str | None = "") -> str:
    ticks = "`" * max(3, max(map(len, re.findall(r"`+", text)), default=0) + 1)
    return f"{ticks}{language or ''}\n{text.rstrip()}\n{ticks}"


def _attached(names: list[str]) -> str:
    names = [n.strip() for n in names if n.strip()]
    return f"*Attached: {', '.join(names)}*" if names else ""


def _branch(nodes: dict, leaf, parent_of: Callable[[dict], object]) -> list[dict]:
    """The messages from the first one to `leaf`, each found as the one before's parent."""
    path, seen = [], set()
    while leaf in nodes and leaf not in seen:
        seen.add(leaf)
        path.append(nodes[leaf])
        leaf = parent_of(nodes[leaf])
    return path[::-1]


def _newest(times: dict) -> object:
    """The message written last, which is where the branch you were on ends when the export doesn't say."""
    return max(times, key=lambda k: times[k], default=None)


# -- Claude ---------------------------------------------------------------------------------------

CLAUDE_LEFT_OUT = {"thinking", "redacted_thinking", "tool_result", "injected_prompt_block", "image", "document"}
SEARCH_TOOLS = {"web_search", "websearch", "web_search_exa"}
FETCH_TOOLS = {"web_fetch", "webfetch", "web_fetch_exa"}
FILE_TOOLS = {"create_file": ("path", "file_text"), "write": ("file_path", "content")}  # where the path and text are
ARTIFACT_LANGUAGES = {"text/html": "html", "application/vnd.ant.react": "jsx", "image/svg+xml": "svg",
                      "application/vnd.ant.mermaid": "mermaid", "text/markdown": "markdown"}


def _claude_tool(name: str, args) -> str:
    """A tool call that shows what the assistant did: a web search or a page read as a line, and a file
    or artifact it wrote in full. What else it ran, and every result, is left out."""
    if not isinstance(args, dict):
        return ""
    base = name.rsplit("__", 1)[-1].lower()  # MCP tools are named mcp__<server>__<tool>
    if base in SEARCH_TOOLS and isinstance(args.get("query"), str):
        return f"*Searched the web: {args['query']}*"
    if base in FETCH_TOOLS:
        urls = [u for u in (args.get("urls") if isinstance(args.get("urls"), list) else [args.get("url")]) if isinstance(u, str)]
        return f"*Read {', '.join(urls)}*" if urls else ""
    if base in FILE_TOOLS:
        path, text = (args.get(k) for k in FILE_TOOLS[base])
        if isinstance(path, str) and isinstance(text, str):
            return f"### {path}\n\n{_fence(text, Path(path).suffix.lstrip('.'))}"
    if base == "artifacts" and args.get("command") in ("create", "rewrite") and isinstance(args.get("content"), str):
        language = args.get("language") or ARTIFACT_LANGUAGES.get(args.get("type"), "")
        return f"### {args.get('title') or 'Artifact'}\n\n{_fence(args['content'], language)}"
    return ""


def _claude_parts(m: dict, unknown: Counter) -> list[str]:
    parts, cited = [], []
    blocks = m.get("content")
    if not isinstance(blocks, list) or not blocks:  # older exports may have only the text
        blocks = [{"type": "text", "text": m.get("text") or ""}]
    for b in blocks:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "text":
            parts.append(b.get("text") or "")
            for c in b.get("citations") or []:
                url = (c.get("details") or {}).get("url") if isinstance(c, dict) else None
                if isinstance(url, str):
                    cited.append(url)
        elif kind == "tool_use":
            parts.append(_claude_tool(str(b.get("name") or ""), b.get("input")))
        elif kind in CLAUDE_LEFT_OUT:
            continue
        elif isinstance(b.get("text"), str):
            parts.append(b["text"])
        else:
            unknown[f"Claude's {kind}"] += 1
    for a in m.get("attachments") or []:
        if isinstance(a, dict) and isinstance(a.get("extracted_content"), str) and a["extracted_content"].strip():
            parts.append(f"### Attached: {a.get('file_name') or 'pasted text'}\n\n{a['extracted_content']}")
    parts.append(_attached([f["file_name"] for f in m.get("files") or []
                            if isinstance(f, dict) and isinstance(f.get("file_name"), str)]))
    if cited:
        parts.append("Sources:\n\n" + "\n".join(f"- {url}" for url in dict.fromkeys(cited)))
    return parts


def _claude(c: dict, unknown: Counter, single: bool) -> Chat | None:
    cid, messages = c.get("uuid"), c.get("chat_messages")
    if not isinstance(cid, str) or not isinstance(messages, list):
        return None
    nodes = {m["uuid"]: m for m in messages if isinstance(m, dict) and isinstance(m.get("uuid"), str)}
    times = {k: _time(m.get("created_at")) or "" for k, m in nodes.items()}
    if any(m.get("parent_message_uuid") for m in nodes.values()):
        leaf = c.get("current_leaf_message_uuid")
        branch = _branch(nodes, leaf if leaf in nodes else _newest(times), lambda m: m.get("parent_message_uuid"))
    else:  # from before branches were kept
        branch = list(nodes.values())
    turns = [Turn(m.get("sender") == "human", times[m["uuid"]] or None, _claude_parts(m, unknown)) for m in branch]
    model = c.get("model")
    return Chat("claude", cid, str(c.get("name") or ""), _time(c.get("created_at")), _time(c.get("updated_at")),
                max(times.values(), default=""), len(nodes), turns, "chat:claude:extension" if single else "chat:claude",
                rank=int("current_leaf_message_uuid" in c), model=model if isinstance(model, str) else None)


# -- ChatGPT --------------------------------------------------------------------------------------

CHATGPT_LEFT_OUT = {"thoughts", "reasoning_recap", "user_editable_context", "model_editable_context", "system_error"}
MARKER = re.compile("\ue200(\\w+)\ue202(.*?)\ue201", re.S)  # \ue200kind\ue202argument\ue202...\ue201
MARKS = re.compile("[\ue200-\ue20f]")


def _unmark(text: str) -> str:
    """ChatGPT's text without its hidden markers: a citation goes, and a named thing or a link keeps its words."""
    def one(m: re.Match) -> str:
        kind, args = m.group(1), m.group(2).split("\ue202")
        if kind == "entity":  # ["cryptocurrency", "Zcash", 0]
            try:
                value = json.loads(args[0])
            except ValueError:
                return ""
            return str(value[1]) if isinstance(value, list) and len(value) > 1 else ""
        if kind == "url" and len(args) > 1:
            return f"[{args[0]}]({args[1]})"
        if kind in ("navlist", "video"):
            return args[0]
        return ""
    return MARKS.sub("", MARKER.sub(one, text))


def _chatgpt_parts(content, meta: dict, unknown: Counter) -> list[str]:
    if not isinstance(content, dict):
        return []
    kind, parts, images = content.get("content_type"), [], 0
    if kind in ("text", "multimodal_text"):
        for part in content.get("parts") or []:
            if isinstance(part, str):
                parts.append(_unmark(part))
            elif isinstance(part, dict) and isinstance(part.get("text"), str):  # a voice message's words
                parts.append(part["text"])
            elif isinstance(part, dict) and "image" in str(part.get("content_type")):
                images += 1
    elif kind == "code" and isinstance(content.get("text"), str):
        parts.append(_fence(content["text"], content.get("language") if isinstance(content.get("language"), str) else ""))
    elif kind not in CHATGPT_LEFT_OUT:
        if isinstance(content.get("text"), str):
            parts.append(content["text"])
        else:
            unknown[f"ChatGPT's {kind}"] += 1
    names = [a["name"] for a in meta.get("attachments") or [] if isinstance(a, dict) and isinstance(a.get("name"), str)]
    if not names and images:
        names = ["an image" if images == 1 else f"{images} images"]
    parts.append(_attached(names))
    return parts


def _chatgpt(c: dict, unknown: Counter, single: bool) -> Chat | None:
    cid, mapping = c.get("conversation_id") or c.get("id"), c.get("mapping")
    if not isinstance(cid, str) or not isinstance(mapping, dict):
        return None
    nodes = {k: n for k, n in mapping.items() if isinstance(n, dict)}
    said = {k: n["message"] for k, n in nodes.items() if isinstance(n.get("message"), dict)}
    times = {k: _time(m.get("create_time")) or "" for k, m in said.items()}
    leaf = c.get("current_node")
    turns = []
    for node in _branch(nodes, leaf if leaf in nodes else _newest(times), lambda n: n.get("parent")):
        m = node.get("message")
        if not isinstance(m, dict):
            continue
        role = (m.get("author") or {}).get("role")
        meta = m.get("metadata") if isinstance(m.get("metadata"), dict) else {}
        # system messages, tools' answers, and the assistant's calls to its tools aren't part of the conversation
        if role not in ("user", "assistant") or meta.get("is_visually_hidden_from_conversation") \
                or m.get("recipient") not in ("all", None):
            continue
        model = meta.get("model_slug") if role == "assistant" and isinstance(meta.get("model_slug"), str) else None
        turns.append(Turn(role == "user", _time(m.get("create_time")), _chatgpt_parts(m.get("content"), meta, unknown), model))
    return Chat("chatgpt", cid, str(c.get("title") or ""), _time(c.get("create_time")), _time(c.get("update_time")),
                max(times.values(), default=""), len(said), turns, "chat:chatgpt")


# -- DeepSeek -------------------------------------------------------------------------------------

DEEPSEEK_LEFT_OUT = {"THINK", "TOOL_FIND", "TEMPLATE_RESPONSE", "CONTENT_RISK"}  # the last two: "server busy", a warning


def _deepseek_parts(fragments, unknown: Counter) -> list[str]:
    parts = []
    for f in fragments if isinstance(fragments, list) else []:
        if not isinstance(f, dict):
            continue
        kind = f.get("type")
        if kind in ("REQUEST", "RESPONSE"):
            parts.append(f.get("content") or "")
        elif kind == "FILE":
            parts.append(_attached([x["file_name"] for x in f.get("files") or []
                                    if isinstance(x, dict) and isinstance(x.get("file_name"), str)]))
        elif kind == "TOOL_SEARCH":
            queries = [q for q in f.get("queries") or [] if isinstance(q, str)]
            parts.append(f"*Searched the web: {'; '.join(queries)}*" if queries else "")
        elif kind == "TOOL_OPEN":
            parts.append(f"*Read {f['url']}*" if isinstance(f.get("url"), str) else "")
        elif kind in DEEPSEEK_LEFT_OUT:
            continue
        elif isinstance(f.get("content"), str):
            parts.append(f["content"])
        else:
            unknown[f"DeepSeek's {kind}"] += 1
    return parts


def _deepseek(c: dict, unknown: Counter, single: bool) -> Chat | None:
    cid, messages = c.get("id"), c.get("messages")
    if not isinstance(cid, str) or not isinstance(messages, list):
        return None
    nodes = {m["id"]: m for m in messages if isinstance(m, dict) and isinstance(m.get("id"), (int, str))}
    times = {k: _time(m.get("inserted_at")) or "" for k, m in nodes.items()}
    leaf = c.get("current_message_id")
    turns = [Turn(m.get("role") == "USER", times[m["id"]] or None, _deepseek_parts(m.get("fragments"), unknown))
             for m in _branch(nodes, leaf if leaf in nodes else _newest(times), lambda m: m.get("parent_id"))]
    return Chat("deepseek", cid, str(c.get("title") or ""), _time(c.get("inserted_at")), _time(c.get("updated_at")),
                max(times.values(), default=""), len(nodes), turns, "chat:deepseek")


PARSERS = {"claude": _claude, "chatgpt": _chatgpt, "deepseek": _deepseek}


def _kind(value) -> str | None:
    """Which site's conversation this is, by its fields."""
    if not isinstance(value, dict):
        return None
    if "chat_messages" in value:
        return "claude"
    if "mapping" in value and ("current_node" in value or "conversation_id" in value):
        return "chatgpt"
    messages = value.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], dict) and "fragments" in messages[0]:
        return "deepseek"
    return None


# -- files ----------------------------------------------------------------------------------------

def find_files(paths: list[Path]) -> list[Path]:
    """The files given, and the JSON and zip files in the folders given and their subfolders, hidden ones left out."""
    from .sessions import side_file
    found = set()
    for p in paths:
        for f in [p] if p.is_file() else p.rglob("*"):
            if f.is_file() and f.suffix.lower() in SUFFIXES and not any(part.startswith(".") for part in f.parts[len(p.parts):]) \
                    and not (f.suffix.lower() == ".jsonl" and side_file(f)):
                found.add(f)
    return sorted(found)


def _sources(path: Path) -> Iterator[tuple[str, int, Callable[[], BinaryIO]]]:
    """(name, size, opener) for a JSON file, or for each JSON file inside a zip. Zips are read where they
    are, never unpacked."""
    if path.suffix.lower() != ".zip":
        yield str(path), path.stat().st_size, lambda: open(path, "rb")
        return
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if not info.is_dir() and info.filename.lower().endswith(".json") and not info.filename.startswith("__MACOSX/"):
                yield f"{path}:{info.filename}", info.file_size, lambda info=info: z.open(info)


def _values(size: int, raw: BinaryIO, name: str) -> Iterator[tuple[object, bool]]:
    """(value, whether the file is that one value) for a JSON object, or for each element of a JSON array,
    decoding one at a time. An object bigger than OBJECT_LIMIT is passed over, since it's read whole."""
    text = _Json(io.TextIOWrapper(raw, encoding="utf-8-sig"))
    try:
        first = text.peek()
    except ValueError:  # empty
        return
    if first == "{":
        if size > OBJECT_LIMIT:
            log.warning("%s: %d MB in one piece, too big to read, passed over", name, size >> 20)
            return
        yield text.decode(), True
    elif first == "[":
        text.pos += 1
        while text.peek() != "]":
            yield text.decode(), False


def _chats_in(name: str, size: int, opener: Callable[[], BinaryIO], unknown: Counter) -> Iterator[Chat | None]:
    """Each conversation in one JSON file, or None for one that couldn't be read. Nothing if it holds none:
    an array is given up on at its first element."""
    with opener() as raw:
        kind = None
        for value, single in _values(size, raw, name):
            if kind is None and not (kind := _kind(value)):
                return
            chat = None
            if _kind(value) == kind:
                try:  # a change in the format should cost that conversation, not the rest of the file
                    chat = PARSERS[kind](value, unknown, single)
                except (TypeError, AttributeError, KeyError, IndexError) as e:
                    log.warning("%s: couldn't read a conversation: %s: %s", name, type(e).__name__, e)
            yield chat


def survey(paths: list[Path]) -> Counter:
    """{(provider, one conversation per file): files} for the folders, reading no more of each file than
    it takes to tell."""
    from . import sessions
    found: Counter = Counter()
    for path in find_files(paths):
        if path.suffix.lower() == ".jsonl":
            if what := sessions.kind(path):
                found["claude-code" if what == "history" else what, what != "history"] += 1
            continue
        try:
            for name, size, opener in _sources(path):
                with opener() as raw:
                    for value, single in _values(size, raw, name):
                        if kind := _kind(value):
                            found[kind, single] += 1
                        break
        except READ_ERRORS:
            continue
    return found


def describe(found: Counter) -> str:
    return ", ".join(f"{n:,} {SURVEY[k][n != 1]}" for k, n in sorted(found.items(), key=lambda kv: list(SURVEY).index(kv[0])))


# -- documents ------------------------------------------------------------------------------------

def markdown(chat: Chat) -> str:
    """The branch you were on, with a heading for each turn: who, when (in UTC) and the model where the
    export names it. Messages in a row from the same side are one turn, unless the chat says otherwise."""
    name = PROVIDERS[chat.provider][0]
    turns: list[Turn] = []
    for turn in chat.turns:
        parts = [p.strip() for p in turn.parts if p and p.strip()]
        if not parts:
            continue
        who = turn.name or ("You" if turn.you else name)
        if chat.merge and turns and turns[-1].name == who:
            turns[-1].parts += parts
        else:
            turns.append(Turn(turn.you, turn.when, parts, turn.model, who))
    return "\n\n".join(f"## {' · '.join(x for x in (t.name, _shown(t.when), t.model) if x)}\n\n"
                       + "\n\n".join(t.parts) for t in turns)


def _title(chat: Chat) -> str:
    """Its own title, or the start of your first message."""
    if chat.title.strip():
        return chat.title.strip()
    for turn in chat.turns:
        if turn.you:
            for line in "\n".join(turn.parts).splitlines():
                if line := line.strip(" #*>-"):
                    return line if len(line) <= TITLE_CHARS else line[:TITLE_CHARS].rsplit(" ", 1)[0] + "…"
    return "Untitled chat"


class Saver:
    """Writes each conversation unless the archive has a copy at least as new."""

    def __init__(self, store: Store):
        self.store = store
        self.saved = {row[0]: row[1:] for row in store.db.execute(
            "SELECT doc_key, newest, updated, messages, rank, reader, model FROM chats")}
        self.deleted = {key for (key,) in store.db.execute(
            "SELECT DISTINCT doc_key FROM urls WHERE doc_key LIKE 'chat:%' AND reason = 'deleted'")}
        self.ends = {row[0]: row[1:] for row in store.db.execute("SELECT doc_key, first, last FROM chat_ends")}
        self.outcomes: Counter = Counter()

    def save(self, chat: Chat | None) -> None:
        self.outcomes[self._save(chat)] += 1

    def has_whole(self, chat: Chat) -> bool:
        """Whether a session's own log is saved, or was a copy of a longer session's, either of which makes
        its prompt history redundant."""
        old = self.saved.get(chat.key)
        if old and old[3] >= 1 and self.store.md_path(doc_id(chat.key)).is_file():
            return True
        row = self.store.db.execute("SELECT doc_key FROM urls WHERE url = ?", (chat.url,)).fetchone()
        return bool(row and row[0] and row[0] != chat.key)

    def _copy_of(self, chat: Chat) -> str | None:
        """Another saved session that holds this one whole, its first message and its last."""
        row = self.store.db.execute(
            "SELECT doc_key FROM chat_messages WHERE message = ? AND doc_key != ?"
            " INTERSECT SELECT doc_key FROM chat_messages WHERE message = ?", (chat.ids[-1], chat.key, chat.ids[0])).fetchone() \
            if chat.ids else None
        return row[0] if row else None

    def _lead_to(self, url: str, key: str) -> None:
        """A copy's address leads to the document of the session holding it."""
        row = self.store.db.execute("SELECT status, reason, chars FROM urls WHERE doc_key = ? LIMIT 1", (key,)).fetchone()
        status, reason, chars = row or ("ok", None, None)
        self.store.db.execute("INSERT OR IGNORE INTO urls (url, lane, status, dedup_key) VALUES (?, ?, ?, ?)", (url, LANE, status, url))
        self.store.db.execute("UPDATE urls SET lane = ?, doc_key = ?, doc_id = ?, status = ?, reason = ?, chars = ?, title = ?,"
                              " updated_at = ? WHERE url = ?",
                              (LANE, key, doc_id(key), status, reason, chars, self.store.title_of(doc_id(key)), now_iso(), url))

    def _take_over_copies(self, chat: Chat) -> None:
        """Saved sessions this one holds whole, from their first message to their last: their links now
        lead to its document, and theirs goes. A session that only starts inside another, as one continued
        from it can, is no copy."""
        ids = set(chat.ids)
        for key, (first, last) in list(self.ends.items()):
            if key != chat.key and first in ids and last in ids:
                self.store.db.execute("UPDATE urls SET doc_key = ?, doc_id = ? WHERE doc_key = ?", (chat.key, doc_id(chat.key), key))
                self.store.md_path(doc_id(key)).unlink(missing_ok=True)
                for table in ("chats", "chat_messages", "chat_ends"):
                    self.store.db.execute(f"DELETE FROM {table} WHERE doc_key = ?", (key,))
                self.saved.pop(key, None)
                self.ends.pop(key)
                self.outcomes["an older copy of a session saved, which now leads to the longer one"] += 1
        self.store.db.execute("DELETE FROM chat_messages WHERE doc_key = ?", (chat.key,))
        self.store.db.executemany("INSERT OR IGNORE INTO chat_messages VALUES (?, ?)", ((m, chat.key) for m in ids))
        self.store.db.execute("INSERT OR REPLACE INTO chat_ends VALUES (?, ?, ?)", (chat.key, chat.ids[0], chat.ids[-1]))
        self.ends[chat.key] = (chat.ids[0], chat.ids[-1])

    def _save(self, chat: Chat | None) -> str:
        if chat is None:
            return "couldn't be read"
        key = chat.key
        if key in self.deleted:
            return "deleted from the archive before, left alone"
        if holder := self._copy_of(chat):
            self._lead_to(chat.url, holder)
            return "a copy of a longer session saved, skipped"
        body = markdown(chat)
        if not body:
            return "no messages, skipped"
        old = self.saved.get(key)
        here = old is not None and self.store.md_path(doc_id(key)).is_file()
        if here:
            newest, updated, messages, rank, reader, _ = old
            known = (newest or "", updated or "", messages, rank)
            same = chat.fresh[:3] == known[:3]
            if chat.messages < messages:
                return "fewer messages than the copy saved, left alone"
            if not same and chat.fresh < known:
                return "older than the copy saved, left alone"
            if same and (chat.rank < rank or (chat.rank == rank and reader >= chat.reader)):
                return "unchanged"
        model = chat.model or (old[5] if old else None)  # the extension's copy named it, and a later export doesn't
        meta = {"url": chat.url, "source": chat.source, "title": _title(chat), "date": _shown(chat.created, seconds=True),
                "site": urlsplit(chat.url).hostname, "model": model, "folder": chat.folder}
        chars = self.store.write_doc(key, meta, body)
        ok = chars >= extract.MIN_CHARS
        if chat.ids:
            self._take_over_copies(chat)
        r = route(chat.url)  # None for a session's address
        url, dedup = (r.url, r.dedup_key) if r else (chat.url, chat.url)
        self.store.record_import(url, key, dedup, "ok" if ok else "thin", None if ok else "short", chars, None, lane=LANE)
        self.store.db.execute("INSERT OR REPLACE INTO chats VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (key, chat.newest, chat.updated, chat.messages, chat.rank, chat.reader, model))
        self.saved[key] = (chat.newest, chat.updated, chat.messages, chat.rank, chat.reader, model)
        if not here:
            return "saved"
        if not same:
            return "updated from a newer copy"
        return "replaced by a copy that knows the branch you were on" if chat.rank > rank \
            else "written again, as this version of osiris writes them differently"


def _link_old_rows(store: Store) -> None:
    """Links to conversations that your tab backups brought in before such links had a document of their
    own get one now, so that the conversation's document takes them over."""
    rows = store.db.execute("SELECT url FROM urls WHERE lane = 'skip' AND reason = 'private_app' AND doc_key IS NULL").fetchall()
    store.db.executemany("UPDATE urls SET doc_key = ?, doc_id = ? WHERE url = ?",
                         [(r.doc_key, r.doc_id, url) for (url,) in rows if (r := route(url)) and r.doc_key])
    store.commit()


def _read(path: Path, unknown: Counter) -> Iterator[Chat | None]:
    from . import sessions
    if path.suffix.lower() == ".jsonl":
        yield from sessions.read(path, unknown)
        return
    for name, size, opener in _sources(path):
        yield from _chats_in(name, size, opener, unknown)


def import_files(store: Store, paths: list[Path]) -> None:
    from . import sessions
    # Claude Code's prompt history last, so that it can tell the sessions whose logs are saved
    files = sorted(find_files(paths), key=lambda f: f.name == "history.jsonl")
    log.info("%d JSON, JSONL and zip files found", len(files))
    _link_old_rows(store)
    read = {key for (key,) in store.db.execute("SELECT key FROM chat_files")}
    saver = Saver(store)
    unknown: Counter = Counter()
    skipped = 0
    try:
        for path in files:
            try:
                session = path.suffix.lower() == ".jsonl"
                key = f"{'s' + str(sessions.READER) if session else READER}:{file_sha1(path)}"
                if key in read:
                    skipped += 1
                    continue
                n = 0
                for chat in _read(path, unknown):
                    if chat is not None and chat.rank == 0 and chat.provider == "claude-code" and saver.has_whole(chat):
                        continue  # a prompt history's session whose log is saved
                    saver.save(chat)
                    n += 1
                if n > 1 and not session:  # DeepSeek's export, and the extension's files, hold one each
                    log.info("%s: %d conversations", path, n)
                store.db.execute("INSERT OR REPLACE INTO chat_files VALUES (?, ?, ?)", (key, str(path.resolve()), now_iso()))
                store.commit()
            except READ_ERRORS as e:
                log.warning("%s: stopped reading it: %s", path, e)
    finally:
        store.commit()
        if skipped:
            log.info("%d files read before and unchanged since, skipped", skipped)
        if unknown:  # a sign the format has changed
            log.warning("left out, since osiris doesn't know these parts of a conversation yet: %s",
                        ", ".join(f"{kind} ({n:,})" for kind, n in unknown.most_common()))
        for outcome, n in saver.outcomes.most_common():
            print(f"{n:>7}  {outcome}")
