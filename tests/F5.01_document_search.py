"""FB1/FB2/FB3/FB4: real local FTS5, durable revisions, bounded mutations and event routing."""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from blocs.document_search import logic
from blocs.document_search.block import DocumentSearchBlock
from bloxsmith_app.block_api import BlockRuntimeContext


def rejected(call):
    """Invalid/cancelled/conflicting requests must fail explicitly."""
    try:
        call()
    except (logic.SearchError, sqlite3.Error, OSError):
        return
    raise AssertionError("Invalid request accepted")


def document(ident="guide", rev=1, content="Restart the café service after installation."):
    """A source reference travels unchanged through tokenization and indexing."""
    return {"id": ident, "revision": rev, "title": "Guide", "metadata": {"lang": "en"},
            "sections": [{"id": "p3", "text": content, "page": 3, "source": {"file": "guide.pdf", "sha256": "fixture"}}]}


def main():
    """No fixtures are loaded from a user's index or source directory."""
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        config = logic.configuration({})
        run = lambda value, port, options=config: logic.process(value, port, options, directory)
        first = run(document(), "documents")
        assert first["status"]["documents"] == 1 and first["status"]["index_revision"] == 1
        assert "matches" not in first and first["status"]["source_freshness"] == "not_checked"
        retry = run(document(), "documents")
        assert retry["status"]["operation"]["unchanged"] == ["guide"] and retry["status"]["index_revision"] == 1
        matches = run("cafe restart", "query")["matches"]["items"]
        assert len(matches) == 1 and matches[0]["reference"]["page"] == 3
        assert matches[0]["reference"]["source"] == {"file": "guide.pdf", "sha256": "fixture"}
        assert matches[0]["document_revision"] == 1 and matches[0]["metadata"] == {"lang": "en"}
        assert matches[0]["score"] > 0 and "Restart" in matches[0]["excerpt"]
        assert run("unknown", "query")["matches"]["items"] == []
        assert run({"text": "cafe", "document_ids": []}, "query")["matches"]["items"] == []
        assert run({"text": "cafe", "document_ids": ["other"]}, "query")["matches"]["items"] == []
        assert len(run("missing cafe", "query", {**config, "match_mode": "any_term"})["matches"]["items"]) == 1
        assert not run("cafe restart", "query", {**config, "match_mode": "phrase"})["matches"]["items"]
        assert len(run("cafe service", "query", {**config, "match_mode": "phrase"})["matches"]["items"]) == 1
        assert not run('"; DROP TABLE documents; --', "query")["matches"]["items"]
        assert not run("!!!", "query")["matches"]["items"]
        rejected(lambda: run(document(content="different"), "documents"))
        rejected(lambda: run({"documents": [document("new"), document(content="conflict")]}, "documents"))
        assert run({"action": "status"}, "command")["status"]["known_documents"] == 1
        run(document(rev=2, content="A replacement text with a <script>alert(1)</script> literal."), "documents")
        assert run("restart", "query")["matches"]["items"] == []
        assert "<script>" in run("alert", "query")["matches"]["items"][0]["excerpt"]
        rejected(lambda: run({"action": "remove", "expected_revisions": {"guide": 1}}, "command"))
        removed = run({"action": "remove", "expected_revisions": {"guide": 2}}, "command")
        assert removed["status"]["operation"]["removed"] == [{"id": "guide", "revision": 3}]
        assert removed["status"]["documents"] == 0 and removed["status"]["known_documents"] == 1
        assert not run("replacement", "query")["matches"]["items"]
        rejected(lambda: run(document(rev=2), "documents"))
        rejected(lambda: run(document(rev=3), "documents"))
        run(document(rev=4), "documents")
        before = run({"action": "status"}, "command")["status"]["index_revision"]
        rebuilt = run({"action": "rebuild"}, "command")
        assert rebuilt["status"]["index_revision"] == before + 1
        assert len(run("cafe", "query")["matches"]["items"]) == 1
        # One of two conflicting writes may commit; the other cannot overwrite it.
        def race(index):
            try:
                run(document(rev=5, content="writer " + str(index)), "documents")
                return True
            except logic.SearchError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(race, [1, 2])) == 1
        assert run("writer", "query")["matches"]["items"][0]["document_revision"] == 5
        rejected(lambda: run(document("extra"), "documents", {**config, "max_documents": 1}))
        rejected(lambda: run({"documents": [document("a"), document("b")]}, "documents",
                             {**config, "max_sections": 1}))
        assert run({"action": "status"}, "command")["status"]["known_documents"] == 1
        long = document("long", content="x" * 1025)
        rejected(lambda: run(long, "documents", {**config, "max_characters": 1024}))
        rejected(lambda: logic.process(document("cancel"), "documents", config, directory, cancel=lambda: True))
        rejected(lambda: logic.process("writer", "query", config, directory, deadline=time.monotonic() - 1))
        for bad in ({}, {"text": ""}, {"text": "x", "unknown": 1}, {"text": "x " * 33}, {"text": "x", "document_ids": ["../escape"]}):
            rejected(lambda: run(bad, "query"))
        for bad in ('{"id":"x","id":"y"}', {"id": "x", "revision": True, "sections": []},
                    document(content="x\x00y"), {**document(), "unknown": 1}):
            rejected(lambda: run(bad, "documents"))
        for options in ({"match_mode": []}, {"max_results": True}, {"corpus": "../x"}, {"unknown": 1}):
            rejected(lambda: logic.configuration(options))
        for action in ({"action": []}, {"action": "clear"}, {"action": "remove", "expected_revisions": {"guide": 5, "missing": 1}}):
            rejected(lambda: run(action, "command"))
        assert run("writer", "query")["matches"]["items"]
        files = list(directory.glob("corpus-*.sqlite3"))
        assert len(files) == 1 and files[0].stat().st_mode & 0o777 == 0o600
        # Separate configured corpus, never selected by an input message.
        assert run("writer", "query", {**config, "corpus": "other"})["matches"]["items"] == []
        # New-event routing must ignore a latched destructive command; port ids are reordered.
        block = DocumentSearchBlock()
        inp = tuple(SimpleNamespace(id=ident, name=name) for ident, name in ((9, "documents"), (7, "query"), (4, "command")))
        out = (SimpleNamespace(id=8, name="matches"), SimpleNamespace(id=5, name="status"))
        events = tuple(SimpleNamespace(input_port_id=7, value=value) for value in ("writer", "missing"))
        context = BlockRuntimeContext(config=config, input_ports=inp, output_ports=out,
            inputs={"command": '{"action":"remove","expected_revisions":{"guide":5}}'},
            input_events=events, services={"get_block_storage_dir": lambda: directory})
        result = block.execute_runtime(context)
        values = {item.port_name: json.loads(item.value) for item in result.outputs}
        assert result.status == "success" and len(values["matches"]["results"]) == 2
        assert values["matches"]["results"][0]["items"] and not values["matches"]["results"][1]["items"]
        assert len(values["status"]["events"]) == 2 and not values["status"]["failed"]
        assert run("writer", "query")["matches"]["items"]
        context.input_events = (SimpleNamespace(input_port_id=7, value='{"text":""}'),)
        failure = block.execute_runtime(context)
        assert failure.status == "success" and [x.port_name for x in failure.outputs] == ["status"]
        assert json.loads(failure.outputs[0].value)["failed"] == 1
        # Corruption is not treated as an empty index or silently deleted.
        files[0].write_bytes(b"not a database")
        damaged = files[0].read_bytes()
        rejected(lambda: run("writer", "query"))
        assert files[0].read_bytes() == damaged
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        def provision(n):
            try:
                logic.process({"action": "status"}, "command", {"corpus": "c" + str(n)}, directory)
                return True
            except logic.SearchError:
                return False
        with ThreadPoolExecutor(max_workers=10) as pool:
            assert sum(pool.map(provision, range(10))) == 8
        assert len(list(directory.glob("corpus-*.sqlite3"))) == 8
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        sentinel = directory / "sentinel"
        sentinel.write_text("preserve")
        (directory / ".document-search.lock").symlink_to(sentinel)
        rejected(lambda: logic.process("query", "query", {}, directory))
        assert sentinel.read_text() == "preserve"
    print("[ok] Document Search exact revisions, real FTS5, persistence, quotas and event routing")


if __name__ == "__main__":
    main()
