"""Persistent source-aware search without a provider or implicit filesystem crawler."""

import json
import sqlite3
import time
from bloxsmith_app.block_api import APPLICATION_JSON, BlockDefinition, BlockRuntimeOutput, BlockRuntimeResult
from . import logic, ui


# FB1 - Index exact bounded document revisions atomically and reject stale/conflicting mutations.
# FB2 - Search literal text with stable source references, ranking and explicit empty results.
# FB3 - Keep persistent corpora isolated, bounded, cancellable and recoverable without silent resets.
# FB4 - Consume only new named input events and preserve all per-activation replies in both runtimes.
# FB5 - Provide responsive translated modal/inspector drafts and release-owned assets.
class DocumentSearchBlock(BlockDefinition):
    """Local retrieval supplies excerpts, not generated answers or freshness guarantees."""

    kind = "document_search"

    def ui_assets(self, surface="modal"):
        """Use the release manifest for every installation origin."""
        return list(self.model.get("ui_assets", {}).get(surface, []))

    def execute_runtime(self, context):
        """Each event is transactional; a failed request never fabricates empty matches."""
        try:
            config = logic.configuration(context.config)
            if context.input_events:
                ports = {int(port.id): str(port.name) for port in context.input_ports}
                messages = [(ports.get(event.input_port_id), event.value) for event in context.input_events]
            else:
                messages = [(name, context.input_value(name, ident))
                            for name, ident in (("documents", "1"), ("query", "2"), ("command", "3"))
                            if context.has_input_value(name, ident)]
                if len(messages) > 1:
                    raise logic.SearchError("Manual execution with several populated inputs is ambiguous; send one event.")
            if not messages:
                return BlockRuntimeResult(status="skipped", outputs=[])
            if len(messages) > 16:
                raise logic.SearchError("At most 16 input events are accepted per activation.")
            resolver = context.services.get("get_block_storage_dir")
            if not callable(resolver):
                raise logic.SearchError("Persistent block storage is unavailable; no fallback path is used.")
            directory = resolver()
            cancel = context.services.get("cancel_requested", lambda: False)
            deadline = time.monotonic() + config["timeout_sec"]
            replies, states, failed = [], [], 0
            for sequence, (port, value) in enumerate(messages):
                try:
                    result = logic.process(value, port, config, directory, cancel, deadline)
                    if "matches" in result:
                        replies.append({"event_index": sequence, **result["matches"]})
                    states.append({"event_index": sequence, "input": port, **result["status"]})
                except (ValueError, TypeError, RecursionError, OSError, sqlite3.Error) as exc:
                    failed += 1
                    detail = str(exc) if isinstance(exc, logic.SearchError) else "Index operation failed; no automatic reset was attempted."
                    states.append({"event_index": sequence, "input": port, "status": "error", "detail": detail})
            values = {"status": {"events": states, "failed": failed}}
            if replies:
                values["matches"] = {"results": replies}
            logic.encoded(values, 33554432)
            outputs = [BlockRuntimeOutput(port_id=int(port.id), port_name=port.name,
                       value=json.dumps(values[port.name], ensure_ascii=False, allow_nan=False), content_type=APPLICATION_JSON)
                       for port in context.output_ports if port.name in values]
            return BlockRuntimeResult(status="success", outputs=outputs, content_type=APPLICATION_JSON,
                                      metadata={self.kind: {"events": len(messages), "queries": len(replies), "failed": failed}},
                                      logs=[f"[document-search] {len(messages)} event(s), {failed} rejected."])
        except (ValueError, TypeError, RecursionError, RuntimeError, OSError, sqlite3.Error) as exc:
            detail = str(exc) if isinstance(exc, logic.SearchError) else "Search configuration or storage is unavailable."
            message = self.translate("block.document_search.error", {"detail": detail}, fallback="Document search failed: {detail}")
            return BlockRuntimeResult(status="failed", outputs=[], error=message, exit_code=1,
                                      logs=[f"[document-search-error] {detail}"])

    def handle_ui_action(self, *, node, action, values, payload=None):
        """Settings validation never opens or removes a corpus."""
        if action in {"save_settings", "modal_update_fields", "inspector_update_fields"}:
            patch = (values or {}).get("node_patch") or {}
            if "config" in patch:
                try:
                    logic.configuration({**self.default_config(), **(node.get("config") or {}), **patch["config"]})
                except (ValueError, TypeError) as exc:
                    return {"error": self.translate("block.document_search.error", {"detail": str(exc)},
                                                  fallback="Document search failed: {detail}")}
        result = super().handle_ui_action(node=node, action="modal_update_fields" if action == "save_settings" else action,
                                          values=values, payload=payload)
        return ui.replace_config(result, node)

    def render_modal(self, *, node, payload=None):
        """Settings and advanced limits stay separate from retrieved private text."""
        return ui.modal(self, node, payload)

    def render_inspector_panel(self, *, node, payload=None):
        """Expose identical policy in a compact keyboard-accessible form."""
        return ui.inspector(self, node, payload)

    def render_node_card(self, *, node, payload=None):
        """Only show corpus identity on the canvas, never indexed contents."""
        return ui.card(self, node, (node.get("config") or {}).get("corpus", "default"))
