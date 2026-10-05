"""Bounded local FTS5 corpus with explicit document revisions and no provider calls."""

from contextlib import contextmanager
import hashlib
import json
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

DEFAULTS = {"corpus": "default", "match_mode": "all_terms", "max_results": 10,
            "max_documents": 1000, "max_sections": 10000, "max_characters": 10000000,
            "max_input_bytes": 1048576, "timeout_sec": 10}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
DB_LIMIT = 134217728


class SearchError(ValueError):
    """An explicit request/storage failure, never an invented empty result."""


def identifier(value):
    """Restrict stable identifiers without using them in SQL or filesystem names."""
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise SearchError("Identifiers require 1–128 ASCII letters, digits, dot, underscore, colon or hyphen.")
    return value


def configuration(raw):
    """Validate policy without opening an index."""
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS) - {"execution", "runtime_path", "runtime_path_label"}:
        raise SearchError("Unknown setting.")
    result = {**DEFAULTS, **{key: value for key, value in raw.items() if key in DEFAULTS}}
    identifier(result["corpus"])
    if not isinstance(result["match_mode"], str) or result["match_mode"] not in {"all_terms", "any_term", "phrase"}:
        raise SearchError("Unknown match_mode.")
    for key, low, high in (("max_results", 1, 100), ("max_documents", 1, 10000),
            ("max_sections", 1, 50000), ("max_characters", 1024, 20000000),
            ("max_input_bytes", 1024, 4194304), ("timeout_sec", 1, 30)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise SearchError(f"{key} must be an integer between {low} and {high}.")
    return result


def encoded(value, limit):
    """Bound finite JSON, depth and UTF-8 encoding before storage or publication."""
    stack, count = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 20 or count > 50000:
            raise SearchError("JSON structure exceeds depth or element limits.")
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise SearchError("JSON keys must be strings.")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise SearchError("Non-JSON value.")
    try:
        result = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(result.encode("utf-8")) > limit:
            raise SearchError("JSON exceeds its byte limit.")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, SearchError):
            raise
        raise SearchError("A bounded finite UTF-8 JSON value is required.") from exc


def pairs(items):
    """Duplicate input keys are ambiguous rather than last-writer-wins."""
    result = {}
    for key, value in items:
        if key in result:
            raise SearchError("Duplicate JSON property.")
        result[key] = value
    return result


def decode(value, limit):
    """Parse strict JSON while leaving plain-text query handling to its own port."""
    if isinstance(value, str):
        if len(value.encode("utf-8")) > limit:
            raise SearchError("Input exceeds max_input_bytes.")
        try:
            value = json.loads(value, object_pairs_hook=pairs)
        except (ValueError, RecursionError) as exc:
            raise SearchError("A JSON object is required.") from exc
    encoded(value, limit)
    if not isinstance(value, dict):
        raise SearchError("A JSON object is required.")
    return value


def text(value, limit, *, nonempty=False):
    """Validate source text without interpreting HTML, URLs or file paths."""
    if not isinstance(value, str) or len(value) > limit or "\x00" in value or (nonempty and not value.strip()):
        raise SearchError("Missing, oversized or invalid text value.")
    return value


def revision(value):
    """All mutations carry explicit monotonic positive revisions."""
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise SearchError("revision must be an integer between 1 and 2147483647.")
    return value


def documents(value, config):
    """Validate a complete upsert batch before any database changes."""
    request = decode(value, config["max_input_bytes"])
    if "documents" in request:
        if set(request) != {"documents"}:
            raise SearchError("A batch contains documents only.")
        batch = request["documents"]
    else:
        batch = [request]
    if not isinstance(batch, list) or not 1 <= len(batch) <= 100:
        raise SearchError("A batch needs 1–100 documents.")
    result, seen = [], set()
    for item in batch:
        if not isinstance(item, dict) or set(item) - {"id", "revision", "title", "metadata", "sections"}:
            raise SearchError("Unknown document fields.")
        doc_id = identifier(item.get("id"))
        if doc_id in seen:
            raise SearchError("Duplicate document id in batch.")
        seen.add(doc_id)
        rev = revision(item.get("revision"))
        title = text(item.get("title", doc_id), 300)
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            raise SearchError("metadata must be an object.")
        encoded(metadata, 4096)
        sections = item.get("sections")
        if not isinstance(sections, list) or not 1 <= len(sections) <= 500:
            raise SearchError("Each document needs 1–500 sections.")
        rows, section_ids = [], set()
        for index, section in enumerate(sections):
            if not isinstance(section, dict) or set(section) - {"id", "text", "source", "page", "paragraph", "section"}:
                raise SearchError("Sections accept id, text, source, page, paragraph and section only.")
            section_id = identifier(section.get("id", str(index + 1)))
            if section_id in section_ids:
                raise SearchError("Section ids must be unique within a document.")
            section_ids.add(section_id)
            content = text(section.get("text"), 50000)
            reference = {k: v for k, v in section.items() if k not in {"id", "text"}}
            for key in ("page", "paragraph", "section"):
                if key in reference and (type(reference[key]) is not int or not 1 <= reference[key] <= 1000000):
                    raise SearchError("Page/paragraph/section references must be positive integers.")
            if "source" in reference and not isinstance(reference["source"], (str, dict)):
                raise SearchError("source must be a string or object.")
            encoded(reference, 4096)
            rows.append({"id": section_id, "text": content, "reference": reference})
        normalized = {"id": doc_id, "revision": rev, "title": title, "metadata": metadata, "sections": rows}
        digest = hashlib.sha256(encoded(normalized, config["max_input_bytes"]).encode()).hexdigest()
        result.append({**normalized, "digest": digest, "characters": sum(len(row["text"]) for row in rows)})
    return result


def check(deadline, cancel):
    """One deadline covers validation follow-up, locks, index work and serialization."""
    if cancel():
        raise SearchError("Search operation cancelled.")
    if time.monotonic() >= deadline:
        raise SearchError("Search operation exceeded timeout_sec.")


@contextmanager
def index(directory, config, deadline, cancel):
    """Only node-scoped files; serialize writers and keep schema failures recoverable."""
    directory = Path(directory)
    if not directory.is_absolute() or directory.resolve() != directory or not directory.is_dir():
        raise SearchError("Canonical node-scoped persistent storage is required.")
    name = "corpus-" + hashlib.sha256(config["corpus"].encode()).hexdigest() + ".sqlite3"
    path = directory / name
    # Cross-corpus creation is serialized too, so concurrent workers cannot exceed the cap.
    lock_path = directory / ".document-search.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SearchError("A private regular storage lock is required.")
        while True:
            check(deadline, cancel)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.02)
        if not path.exists() and len(list(directory.glob("corpus-*.sqlite3"))) >= 8:
            raise SearchError("This block already has eight corpora; no automatic deletion.")
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.is_symlink():
                raise SearchError("Index and journal symlinks are forbidden.")
            if candidate.exists():
                info = candidate.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > DB_LIMIT:
                    raise SearchError("Index files must be bounded private regular files.")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    finally:
        os.close(lock_fd)
    check(deadline, cancel)
    db = sqlite3.connect(path, timeout=0.1)
    try:
        db.enable_load_extension(False)
        db.execute("PRAGMA busy_timeout=100")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA max_page_count=" + str(DB_LIMIT // db.execute("PRAGMA page_size").fetchone()[0]))
        db.set_progress_handler(lambda: int(cancel() or time.monotonic() >= deadline), 1000)
        while True:
            check(deadline, cancel)
            try:
                db.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            if db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
                raise SearchError("Unknown index database; no reset was attempted.")
            db.execute("CREATE TABLE documents (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, digest TEXT NOT NULL, title TEXT NOT NULL, metadata TEXT NOT NULL, sections INTEGER NOT NULL, characters INTEGER NOT NULL, deleted INTEGER NOT NULL)")
            db.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK(id=1), corpus TEXT NOT NULL, revision INTEGER NOT NULL)")
            db.execute("INSERT INTO settings VALUES(1,?,0)", (config["corpus"],))
            db.execute("CREATE VIRTUAL TABLE passages USING fts5(text, document_id UNINDEXED, section_id UNINDEXED, reference UNINDEXED, tokenize='unicode61 remove_diacritics 2')")
            db.execute("PRAGMA user_version=1")
        elif version != 1:
            raise SearchError("Unsupported index schema; no reset or migration was attempted.")
        row = db.execute("SELECT corpus FROM settings WHERE id=1").fetchone()
        if not row or row[0] != config["corpus"]:
            raise SearchError("Index identity mismatch.")
        yield db
        check(deadline, cancel)
        db.commit()
    except BaseException:
        db.set_progress_handler(None, 0)
        db.rollback()
        raise
    finally:
        db.close()


def status(db, config):
    """Report only indexed facts; source files are never reread or claimed current."""
    count, live, sections, characters = db.execute(
        "SELECT count(*),coalesce(sum(deleted=0),0),coalesce(sum(sections),0),coalesce(sum(characters),0) FROM documents").fetchone()
    return {"corpus": config["corpus"], "index_revision": db.execute("SELECT revision FROM settings WHERE id=1").fetchone()[0],
            "known_documents": count, "documents": live, "sections": sections, "characters": characters,
            "source_freshness": "not_checked", "partial": False}


def upsert(db, batch, config):
    """One atomic batch: reject stale/conflicting revisions and any exceeded corpus limit."""
    changed, unchanged = [], []
    for item in batch:
        previous = db.execute("SELECT revision,digest FROM documents WHERE id=?", (item["id"],)).fetchone()
        if previous and item["revision"] <= previous[0]:
            if item["revision"] == previous[0] and item["digest"] == previous[1]:
                unchanged.append(item["id"])
                continue
            raise SearchError("Stale revision or different content reusing a document revision.")
        db.execute("DELETE FROM passages WHERE document_id=?", (item["id"],))
        db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,digest=excluded.digest,title=excluded.title,metadata=excluded.metadata,sections=excluded.sections,characters=excluded.characters,deleted=0",
            (item["id"], item["revision"], item["digest"], item["title"], encoded(item["metadata"], 4096),
             len(item["sections"]), item["characters"]))
        db.executemany("INSERT INTO passages(text,document_id,section_id,reference) VALUES(?,?,?,?)",
            [(row["text"], item["id"], row["id"], encoded(row["reference"], 4096)) for row in item["sections"]])
        changed.append(item["id"])
    observed = status(db, config)
    if observed["known_documents"] > config["max_documents"] or observed["sections"] > config["max_sections"] or observed["characters"] > config["max_characters"]:
        raise SearchError("Corpus capacity exceeded; the entire batch was rolled back.")
    if changed:
        db.execute("UPDATE settings SET revision=revision+1 WHERE id=1")
    return {"indexed": changed, "unchanged": unchanged}


def query_input(value, config):
    """Offer literal search, not SQL or a pass-through FTS expression language."""
    if isinstance(value, str) and not value.lstrip().startswith("{"):
        request = {"text": value}
    else:
        request = decode(value, config["max_input_bytes"])
    if set(request) - {"text", "document_ids"}:
        raise SearchError("Query accepts text and optional document_ids only.")
    query = text(request.get("text"), 512, nonempty=True)
    identifiers = request.get("document_ids")
    if identifiers is not None:
        if not isinstance(identifiers, list) or len(identifiers) > 100:
            raise SearchError("document_ids must contain at most 100 ids.")
        identifiers = list(dict.fromkeys(identifier(x) for x in identifiers))
    tokens = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    if len(tokens) > 32:
        raise SearchError("A query may contain at most 32 words.")
    if config["match_mode"] == "phrase":
        expression = '"' + " ".join(tokens) + '"' if tokens else ""
    else:
        join = " AND " if config["match_mode"] == "all_terms" else " OR "
        expression = join.join('"' + token + '"' for token in tokens)
    return query, identifiers, expression


def search(db, request, config):
    """Return plain snippets and original references; never fabricate an answer."""
    query, identifiers, expression = request
    matches = []
    if expression and identifiers != []:
        clause, args = "", [expression]
        if identifiers is not None:
            clause = " AND passages.document_id IN (" + ",".join("?" for _ in identifiers) + ")"
            args.extend(identifiers)
        args.append(config["max_results"])
        rows = db.execute("SELECT passages.document_id,passages.section_id,passages.reference,d.revision,d.title,d.metadata,"
            "snippet(passages,0,'','',' … ',32),-bm25(passages) FROM passages JOIN documents d ON d.id=passages.document_id "
            "WHERE passages MATCH ? AND d.deleted=0" + clause +
            " ORDER BY bm25(passages),passages.document_id,passages.section_id LIMIT ?", args).fetchall()
        for row in rows:
            matches.append({"document_id": row[0], "section_id": row[1], "reference": json.loads(row[2]),
                "document_revision": row[3], "title": row[4], "metadata": json.loads(row[5]),
                "excerpt": row[6][:4096], "excerpt_truncated": len(row[6]) > 4096, "score": row[7]})
    return {"query": query, "corpus": config["corpus"],
            "index_revision": db.execute("SELECT revision FROM settings WHERE id=1").fetchone()[0], "items": matches}


def command(db, request):
    """Removal requires exact revisions; rebuild touches only this index, not source files."""
    action = request.get("action")
    if isinstance(action, str) and action in {"status", "rebuild"} and set(request) == {"action"}:
        if action == "rebuild":
            db.execute("INSERT INTO passages(passages) VALUES('rebuild')")
            db.execute("UPDATE settings SET revision=revision+1 WHERE id=1")
        return {"action": action}
    if action != "remove" or set(request) != {"action", "expected_revisions"}:
        raise SearchError("Commands: status, rebuild, or remove with expected_revisions.")
    expected = request["expected_revisions"]
    if not isinstance(expected, dict) or not 1 <= len(expected) <= 100:
        raise SearchError("Remove needs 1–100 document ids and their exact revisions.")
    for doc_id, rev in expected.items():
        identifier(doc_id)
        revision(rev)
        row = db.execute("SELECT revision,deleted FROM documents WHERE id=?", (doc_id,)).fetchone()
        if row != (rev, 0) or rev == 2147483647:
            raise SearchError("Removal revision conflict; nothing was removed.")
    removed = []
    for doc_id, rev in expected.items():
        db.execute("DELETE FROM passages WHERE document_id=?", (doc_id,))
        db.execute("UPDATE documents SET revision=revision+1,digest='',title='',metadata='{}',sections=0,characters=0,deleted=1 WHERE id=?", (doc_id,))
        removed.append({"id": doc_id, "revision": rev + 1})
    db.execute("UPDATE settings SET revision=revision+1 WHERE id=1")
    return {"action": "remove", "removed": removed}


def process(value, port, raw_config, directory, cancel=lambda: False, deadline=None):
    """Validate before opening storage; never report corrupt/unavailable indices as empty."""
    config = configuration(raw_config)
    deadline = min(deadline or float("inf"), time.monotonic() + config["timeout_sec"])
    check(deadline, cancel)
    if port == "documents":
        request = documents(value, config)
    elif port == "query":
        request = query_input(value, config)
    elif port == "command":
        request = decode(value, config["max_input_bytes"])
    else:
        raise SearchError("Unknown input port.")
    with index(directory, config, deadline, cancel) as db:
        values = {}
        if port == "documents":
            action = upsert(db, request, config)
        elif port == "command":
            action = command(db, request)
        else:
            values["matches"] = search(db, request, config)
            action = {"action": "search", "count": len(values["matches"]["items"])}
        values["status"] = {**status(db, config), "operation": action, "status": "ready"}
        encoded(values, 2097152)
        return values
