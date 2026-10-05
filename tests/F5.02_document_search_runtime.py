"""FB1/FB2/FB3/FB4/FB5: persistent indexed text between real Runs and package origins."""

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from blocs.document_search.block import DocumentSearchBlock
from bloxsmith_app.block_api import BlockRuntimeContext
from block_test_packages import install_test_package, surface_payload
from ui_smoke_common import create_project_api, create_run_api, data_edge, display_node, graph_payload, graph_storage_dir, http_json, isolated_server, text_node, wait_for_run_terminal


def main():
    """Index in one Run; query through the same public node storage in the next."""
    block = DocumentSearchBlock()
    assert block.execute_runtime(BlockRuntimeContext(config={})).status == "skipped"
    for mode in ("centralized", "zeromq_active"):
        for origin in (None, "managed", "linked"):
            with isolated_server() as server:
                node = block.build_node_payload(node_id="search", position={"x": 360, "y": 140})
                if origin:
                    model = install_test_package(server, "document_search", origin=origin)
                    node["block_version"] = model["version"]
                    for surface in ("modal", "inspector_panel", "node_card"):
                        html = surface_payload(server, model, node, surface=surface)["html"]
                        assert "{{" not in html
                        if surface != "node_card":
                            assert 'data-block-config-field="corpus"' in html
                def document(value, input_port, output_port):
                    return graph_payload("Local document search", [
                        text_node("source", "Input", json.dumps(value), 80, 140), node,
                        display_node("display", "Result", 700, 140)], [
                        data_edge("input", "source", 1, "search", input_port),
                        data_edge("result", "search", output_port, "display", 1)])
                entry = {"id": "fixture", "revision": 1, "sections": [
                    {"text": "The fixture service restarts successfully.", "source": "fixture.txt", "paragraph": 2}]}
                graph = document(entry, 1, 2)
                project = create_project_api(server, document=graph)["project"]
                run = create_run_api(server, graph, project_id=project["graph_id"], runtime_mode=mode)
                result = wait_for_run_terminal(server, run["run_id"], timeout_sec=30)
                assert result["status"] == "success", (mode, origin, result.get("logs"))
                state = json.loads(result["output_values"]["search:2"]["value"])
                assert not state["failed"] and state["events"][0]["documents"] == 1, state
                query_graph = document({"text": "fixture service"}, 2, 1)
                graph_file = graph_storage_dir(server, project) / "graph.json"
                graph_file.write_text(json.dumps({**query_graph, "graph_id": project["graph_id"]}), encoding="utf-8")
                http_json(server.base_url, f"/api/projects/{project['graph_id']}/graph/reload", method="POST", payload={})
                run = create_run_api(server, query_graph, project_id=project["graph_id"], runtime_mode=mode)
                result = wait_for_run_terminal(server, run["run_id"], timeout_sec=30)
                assert result["status"] == "success", (mode, origin, result.get("logs"))
                replies = json.loads(result["output_values"]["search:1"]["value"])["results"]
                assert len(replies) == 1 and len(replies[0]["items"]) == 1, replies
                item = replies[0]["items"][0]
                assert item["document_id"] == "fixture" and item["document_revision"] == 1
                assert item["reference"] == {"source": "fixture.txt", "paragraph": 2}
                assert result["node_statuses"]["display"] == "success", result["node_statuses"]
            print("[ok] Document Search runtime " + mode + " " + str(origin), flush=True)


if __name__ == "__main__":
    main()
