"""Harvest the bundled Hermes Telegram adapter into hermes_telegram/harvested/.

Usage: python scripts/harvest.py --src PATH_TO_HERMES_GIT_REPOSITORY

The pin is the commit in HERMES_PIN at the repository root. Files are read from that commit's objects in
the given repository (``git show PIN:path``), never from its working tree, so a local edit there cannot
end up labelled as upstream code. The output is regenerated from scratch, so harvested code is never
edited by hand: local behaviour lives in hermes_telegram/ outside harvested/, overriding harvested methods.

What it does:
- copies the sibling modules of plugins/platforms/telegram/ whole;
- splits the TelegramAdapter class of adapter.py into one mixin per concern (METHOD_GROUPS), method bodies
  unchanged, and keeps the module-level code of adapter.py as adapter_module.py;
- writes sdk_names.py: the SDK names check_telegram_requirements() rebinds after a lazy install, which the
  plugin copies into the mixin modules (they imported the values, not the names);
- applies only the mechanical rewrites listed in each file's header: sibling imports become relative,
  the repository-root sys.path insertion is dropped, module loggers keep their upstream names, and
  references to the TelegramAdapter class name point at the mixin that now owns the method.

A method the map does not name stops the run, so an upstream change is assigned on purpose.
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = (ROOT / "HERMES_PIN").read_text(encoding="utf-8").strip()
UPSTREAM = "NousResearch/hermes-agent"
SRC_DIR = "plugins/platforms/telegram"
OUT_DIR = ROOT / "hermes_telegram" / "harvested"

SIBLINGS = [
    "adapter_hint", "dashboard_card", "gateway_env", "inline_picker", "telegram_context",
    "telegram_entities", "telegram_held_inbound", "telegram_ids", "telegram_network",
    "telegram_topic_routing", "telegram_topic_transfer", "update_admission",
]

# Mixin module -> (class name, purpose). Order is the MRO order of the composed adapter.
MIXINS = {
    "adapter_core": ("CoreMixin", "construction, class constants, lifecycle flags, error classifiers"),
    "adapter_access": ("AccessMixin", "sender authorization and pairing"),
    "adapter_topics": ("TopicsMixin", "threads, forum and DM topics"),
    "adapter_transport": ("TransportMixin", "PTB application, polling, webhook, reconnects, connect/disconnect"),
    "adapter_events": ("EventsMixin", "platform events (reactions, edits) and processing reactions"),
    "adapter_prompts": ("PromptsMixin", "Hermes' own prompts and their callbacks: approvals, clarify, pickers"),
    "adapter_pacing": ("PacingMixin", "typing, flood cooldowns, per-chat send lock"),
    "adapter_groups": ("GroupsMixin", "group gating, mentions, bot identity"),
    "adapter_inbound": ("InboundMixin", "inbound handlers, batching, media caching, message events"),
    "adapter_outbound_legacy": (
        "OutboundLegacyMixin",
        "the narrow outbound protocol (MarkdownV2, rich, drafts, media sends); replaced slice by slice",
    ),
}

# Each group lists the TelegramAdapter methods it owns, as of PIN. Class-level statements other than
# methods (docstring, constants) go to adapter_core.
METHOD_GROUPS: dict[str, list[str]] = {
    "adapter_core": [
        "_env_float_clamped", "_teardown_started", "message_len_fn", "__init__", "send_path_degraded",
        "_mark_connected", "_mark_disconnected", "_set_fatal_error", "_is_permanent_fatal",
        "_replacement_telegram_adapter", "_wait_for_reconnection", "_should_drop_delayed_delivery",
        "_notification_kwargs", "_normalize_chat_type", "_fallback_ips", "_looks_like_polling_conflict",
        "_looks_like_auth_error", "_looks_like_network_error", "_exception_graph_matches",
        "_looks_like_connect_timeout", "_looks_like_pool_timeout", "_coerce_bool_extra", "_link_preview_kwargs",
        "_is_bad_request_error", "_is_thread_not_found_error", "_telegram_error_types",
        "_classify_send_exception", "_is_timed_out", "get_chat_info", "_extra_bool", "_extra_str_set",
    ],
    "adapter_access": [
        "_legacy_runner_auth_fn", "_env_allowlist_decision", "_is_callback_user_authorized",
        "_source_from_message_for_auth", "_source_from_reaction_for_auth", "_telegram_auth_env_configured",
        "_should_pass_unauthorized_dm_for_pairing", "_is_user_authorized_from_message", "_callback_authorized",
        "_log_blocked_user",
    ],
    "adapter_topics": [
        "_metadata_thread_id", "_metadata_direct_messages_topic_id", "_metadata_reply_to_message_id",
        "_dm_topic_fallback", "_is_private_dm_topic_send", "_dm_topic_missing_anchor_error",
        "_reply_to_message_id_for_send", "_thread_kwargs_for_send", "_direct_topic_kwargs",
        "_thread_kwargs_for_draft", "_message_thread_id_for_send", "_message_thread_id_for_typing",
        "_prune_stale_dm_topic_binding", "_should_retry_without_dm_topic_reply_anchor",
        "_send_with_dm_topic_reply_anchor_retry", "_create_dm_topic", "create_handoff_thread", "ensure_dm_topic",
        "rename_dm_topic", "_persist_dm_topic_thread_id", "_setup_dm_topics", "set_topic_transfer_fn",
        "_ensure_forum_commands", "_topic_id_or_general", "_effective_message_thread_id",
        "_reload_dm_topics_from_config", "_get_dm_topic_info", "_cache_dm_topic_from_message",
        "_resolve_topic_binding",
    ],
    "adapter_transport": [
        "_drain_polling_connections", "_bounded_request_step", "_orphan_and_rebuild_polling_client",
        "_fence_polling", "_begin_polling_generation", "_record_polling_progress",
        "_observe_polling_request_result", "_record_updates_received", "_instrument_polling_request",
        "_start_polling_once", "_schedule_polling_progress_verifier", "_get_general_request_drain_lock",
        "_drain_general_connections_after_pool_timeout", "_spawn_polling_recovery", "_recovery_in_flight",
        "_schedule_polling_recovery", "_delete_webhook_best_effort", "_await_cold_start_readiness",
        "_start_polling_resilient", "_go_fatal_network", "_stop_updater_or_go_fatal", "_restart_polling_in_task",
        "_handle_polling_network_error", "_polling_heartbeat_loop", "_probe_pending_updates",
        "_escalate_stuck_consumer", "_check_ingress_dispatch_stall", "_check_polling_stall", "_verifier_stale",
        "_verify_polling_after_reconnect", "_disarm_ptb_retry_loop", "_handle_polling_conflict",
        "_handoff_polling_fatal_error", "_bot_identity_refresh_loop", "_start_post_connect_housekeeping",
        "_register_command_menu", "_bot_commands", "_menu_fingerprint", "_run_post_connect_housekeeping",
        "_accept_update", "_fail_update_preparation", "handle_message", "_register_handlers",
        "_wire_plugin_handlers", "_build_ptb_requests", "_initialize_app_with_retries", "_cold_boot_drop_pending",
        "_start_webhook_mode", "_start_polling_mode", "connect", "_set_status_indicator", "_collect_live_tasks",
        "_clear_task_attrs_except", "_cancel_pending_delivery_tasks", "_await_disconnect_step",
        "_restart_task_attr", "_cancel_task_attr", "disconnect",
    ],
    "adapter_events": [
        "_on_platform_update", "_source_for_platform_event_auth", "_normalize_platform_event", "_is_id_like",
        "_normalize_reaction_event", "_normalize_message_edited_event", "_edit_revision", "_reactions_enabled",
        "_set_reaction", "_clear_reactions", "on_processing_start", "on_processing_complete",
    ],
    "adapter_prompts": [
        "_send_control_message", "_send_prompt", "_rows_of_two", "send_update_prompt", "_EA_HEADER",
        "_EA_REASON_LABEL", "_EA_SMART_DENY_LINE", "_ea_escape", "_exec_approval_cmd_budget", "_EA_ACTION_LABELS",
        "_send_exec_approval_prompt", "send_slash_confirm", "send_clarify", "_provider_get_label",
        "send_model_picker", "send_choice_picker", "_edit_result_text", "_handle_choice_picker_callback",
        "_provider_button", "_picker_nav_row", "_picker_back_cancel_row", "_paged_keyboard",
        "_build_provider_keyboard", "_build_model_keyboard", "_picker_edit", "_picker_show_models",
        "_provider_list_text", "_picker_show_providers", "_picker_selection", "_picker_switch", "_parse_page",
        "_handle_model_picker_callback", "_notify_clarify_expired", "_edit_html_quiet", "_edit_md_quiet",
        "_handle_inline_query", "_callback_ctx", "_handle_callback_query", "_claim_callback_state",
        "_handle_exec_approval_callback", "_handle_slash_confirm_callback", "_handle_clarify_callback",
        "_handle_update_prompt_callback", "_handle_gmail_triage_callback",
    ],
    "adapter_pacing": [
        "_is_transient_typing_error", "_record_typing_cooldown", "_typing_in_cooldown", "_chat_send_lock",
        "_record_send_flood_cooldown", "_send_flood_cooldown_remaining", "_chat_outbound_slot_remaining",
        "_hold_chat_outbound_slot", "send_typing", "_retrigger_typing",
    ],
    "adapter_groups": [
        "_telegram_require_mention", "_telegram_observe_unmentioned_group_messages", "_telegram_guest_mode",
        "_telegram_exclusive_bot_mentions", "_telegram_bots_require_mention", "_telegram_free_response_chats",
        "_telegram_free_response_topics", "_telegram_is_free_response_topic", "_telegram_allowed_chats",
        "_telegram_group_allowed_chats", "_telegram_observe_allowed_chats", "_telegram_allowed_topics",
        "_telegram_ignored_threads", "_compile_mention_patterns", "_chat_type_str", "_chat_id_str",
        "_is_group_chat", "_current_bot_username", "_note_bot_username", "_observe_bot_identity_from_message",
        "_bot_identity_is_fresh", "_refresh_bot_identity", "_is_reply_to_bot", "_entity_sources", "_entity_type",
        "_entity_span", "_extract_bot_mention_usernames", "_telegram_entity_text", "_message_mentions_bot",
        "_schedule_bot_identity_recheck", "_explicit_bot_mentions_exclude_self",
        "_message_matches_mention_patterns", "_is_guest_mention", "_clean_bot_trigger_text", "_topic_gates_pass",
        "_bot_sender_suppressed", "_should_observe_unmentioned_group_message",
        "_telegram_group_observe_shared_source", "_telegram_group_observe_attributed_text",
        "_telegram_group_observe_channel_prompt", "_apply_telegram_group_observe_attribution",
        "_observe_unmentioned_group_message", "_is_own_message", "_sender_is_other_bot", "_should_process_message",
        "_effective_update_message", "_gate_or_observe",
    ],
    "adapter_inbound": [
        "_media_message_type", "_download_observed_media", "_cache_observed_media", "_attach_cached",
        "_cache_replied_media", "_observed_media_source", "_append_observed_note", "_surface_media_cache_failure",
        "_build_triggered_event", "_handle_text_message", "_handle_command", "_handle_location_message",
        "_text_batch_key", "_enqueue_text_event", "_flush_buffered", "_text_batch_delay_for", "_flush_text_batch",
        "_photo_batch_key", "_flush_photo_batch", "_merge_into_pending", "_enqueue_photo_event",
        "_route_photo_event", "_ext_from_path", "_cache_inbound_av", "_dispatch_with_text", "_set_cached_media",
        "_cache_inbound_document", "_handle_media_message", "_queue_media_group_event", "_flush_media_group_event",
        "_handle_sticker", "_flatten_rich_inline_text", "_flatten_rich_blocks", "_extract_rich_reply_text",
        "_reply_context", "_build_message_event",
    ],
    "adapter_outbound_legacy": [
        "_content_fits_rich_limits", "_bot_supports_rich", "_has_telegram_desktop_details_math_crash_shape",
        "_has_telegram_desktop_cjk_rich_garble_shape", "_needs_rich_rendering", "_rich_delivery_enabled",
        "_rich_content_ok", "_rich_eligible", "_should_attempt_rich", "prefers_fresh_final_streaming",
        "_rich_transport_available", "streaming_overflow_limit", "_rich_message_payload",
        "_is_rich_capability_error", "_is_rich_fallback_error", "_chunk_reply_routing",
        "_compute_single_send_routing", "_rich_transient_result", "_record_rich_sent", "_try_send_rich",
        "_rich_payload_base", "_rich_rejected", "_try_edit_rich", "_should_attempt_rich_draft",
        "_try_send_rich_draft", "_should_thread_reply", "_send_chunk_markdown_or_plain", "_send_chunk_with_retries",
        "send", "_send_text_locked", "_send_chunks", "_with_partial_send", "_resume_partial_send",
        "send_or_update_status", "_edit_text", "_edit_markdown_or_plain", "edit_message",
        "_truncate_stream_overflow_preview", "_send_overflow_continuation", "_edit_overflow_split",
        "delete_message", "supports_draft_streaming", "send_draft", "_send_message_with_thread_fallback",
        "_missing_media_path_error", "_sniff_raster_format", "_compress_image_to_jpeg",
        "_telegram_media_too_large_note", "_int_or_zero", "_telegram_media_size_allowed", "_media_send_kwargs",
        "_send_media", "_caption_1024", "_send_voice_bubble", "send_voice", "send_multiple_images",
        "send_image_file", "_send_local_file", "_warn_then", "send_document", "send_video", "send_image",
        "send_animation", "format_message", "_escape_bare_bracket",
    ],
}

# Top-level definitions of adapter.py that build or register the upstream class; ours replace them.
DROPPED_TOP_LEVEL = {"TelegramAdapter", "_build_adapter", "register"}

SYS_PATH_HACK = "sys.path.insert(0, str(_Path(__file__).resolve().parents[3]))"
# Import statements only: logger names such as "plugins.platforms.telegram.adapter" stay as they are.
SIBLING_IMPORT = re.compile(r"^(\s*from )plugins\.platforms\.telegram\.(\w+)( import )", re.M)
# A module logger keeps the name it has upstream: "<plugin package>.adapter" for adapter.py's code, and
# "<plugin package>.<module>" for a sibling, without the "harvested" level.
MODULE_LOGGER = "logging.getLogger(__name__)"
UPSTREAM_LOGGER = ('logging.getLogger(__name__.replace(".harvested.", ".", 1)'
                   '.replace(".adapter_module", ".adapter"))')


def rewrite_logger(text: str) -> tuple[str, list[str]]:
    count = text.count(MODULE_LOGGER)
    if not count:
        return text, []
    return text.replace(MODULE_LOGGER, UPSTREAM_LOGGER), ["module logger keeps its upstream name"]


def git_show(repo: Path, path: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "show", f"{PIN}:{path}"], capture_output=True, text=True,
                          check=True).stdout


def git_ls(repo: Path, path: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "ls-tree", "--name-only", PIN, f"{path}/"],
                         capture_output=True, text=True, check=True).stdout
    return [line.rsplit("/", 1)[-1] for line in out.splitlines()]


def header(src_name: str, rewrites: list[str], extra: str = "") -> str:
    lines = [
        f"# Harvested from {UPSTREAM}@{PIN}: {SRC_DIR}/{src_name}",
        "# Copyright (c) 2025 Nous Research. MIT License: see LICENSE.hermes in this directory.",
        "# Generated by scripts/harvest.py. Do not edit: override in hermes_telegram/ outside harvested/.",
    ]
    if extra:
        lines.append(f"# {extra}")
    if rewrites:
        lines.append("# Mechanical rewrites:")
        lines.extend(f"#   - {r}" for r in rewrites)
    return "\n".join(lines) + "\n"


def rewrite_sibling_imports(text: str) -> tuple[str, list[str]]:
    """``from plugins.platforms.telegram.x import`` -> ``from .x import`` (and the same inside methods)."""
    found = sorted({m[1] for m in SIBLING_IMPORT.findall(text)})
    new = SIBLING_IMPORT.sub(lambda m: f"{m.group(1)}.{m.group(2)}{m.group(3)}", text)
    if re.search(r"^\s*import plugins\.platforms\.telegram\b", new, re.M):
        raise SystemExit("unexpected 'import plugins.platforms.telegram.x' form; extend the rewrite")
    return new, [f"sibling import plugins.platforms.telegram.{m} -> relative .{m}" for m in found]


def module_bound_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for sub in ast.walk(target):
                        if isinstance(sub, ast.Name):
                            names.add(sub.id)
            elif isinstance(node, ast.Try):
                visit(node.body)
                for handler in node.handlers:
                    visit(handler.body)
                visit(node.orelse)
                visit(node.finalbody)
            elif isinstance(node, (ast.If, ast.With)):
                visit(node.body)
                visit(getattr(node, "orelse", []))

    visit(tree.body)
    return names


def rebound_sdk_names(tree: ast.Module) -> list[str]:
    """Names ``check_telegram_requirements`` declares ``global``: it rebinds them after a lazy install."""
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "check_telegram_requirements")
    return [name for node in ast.walk(fn) if isinstance(node, ast.Global) for name in node.names]


def split_adapter(text: str) -> dict[str, str]:
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)

    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TelegramAdapter")
    bases = [ast.unparse(b) for b in cls.bases]
    if bases != ["TelegramHeldInboundMixin", "TelegramTopicRoutingMixin", "BasePlatformAdapter"]:
        raise SystemExit(f"TelegramAdapter bases changed upstream: {bases}")

    owner = {name: group for group, names in METHOD_GROUPS.items() for name in names}
    dupes = [n for n in owner if sum(n in v for v in METHOD_GROUPS.values()) > 1]
    if dupes:
        raise SystemExit(f"methods mapped twice: {dupes}")

    # Each class-body statement takes the lines from just after the previous one, so comments above a
    # method travel with it.
    chunks: dict[str, list[str]] = defaultdict(list)
    used_names: dict[str, set[str]] = defaultdict(set)
    method_names = set()
    unmapped = []
    prev_end = cls.body[0].lineno - 1
    first_stmt = True
    for node in cls.body:
        start = (node.decorator_list[0].lineno if getattr(node, "decorator_list", None) else node.lineno)
        begin = prev_end if not first_stmt else start - 1
        first_stmt = False
        segment = "".join(lines[begin:node.end_lineno])
        prev_end = node.end_lineno
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method_names.add(node.name)
            group = owner.get(node.name)
            if group is None:
                unmapped.append(node.name)
                continue
        else:
            group = "adapter_core"
        chunks[group].append(segment)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                used_names[group].add(sub.id)
    if unmapped:
        raise SystemExit(f"methods not in METHOD_GROUPS (assign them): {unmapped}")
    stale = sorted(set(owner) - method_names)
    if stale:
        raise SystemExit(f"METHOD_GROUPS names methods that no longer exist: {stale}")

    # Module-level code: everything outside the class and the dropped definitions, comments included.
    keep = [True] * len(lines)
    for node in tree.body:
        name = getattr(node, "name", None)
        if name in DROPPED_TOP_LEVEL:
            start = node.decorator_list[0].lineno if getattr(node, "decorator_list", None) else node.lineno
            for i in range(start - 1, node.end_lineno):
                keep[i] = False
    module_text = "".join(line for line, k in zip(lines, keep) if k)
    if SYS_PATH_HACK not in module_text:
        raise SystemExit("the sys.path insertion changed upstream; review it")
    module_text = module_text.replace(SYS_PATH_HACK, "pass  # repository-root sys.path insertion dropped by harvest")
    module_text, rewrites = rewrite_sibling_imports(module_text)
    module_text, logger_rewrites = rewrite_logger(module_text)
    module_names = module_bound_names(ast.parse(module_text))
    rebound = rebound_sdk_names(tree)

    out = {
        "adapter_module.py": header(
            "adapter.py", rewrites + logger_rewrites + [
                "repository-root sys.path insertion dropped (the plugin imports Hermes as installed)",
                f"removed {', '.join(sorted(DROPPED_TOP_LEVEL))}: hermes_telegram builds and registers the adapter",
            ], "Module-level code of adapter.py: imports, helpers, constants, registration hooks.",
        ) + module_text,
        "sdk_names.py": header(
            "adapter.py", [], "Generated from the global statements of check_telegram_requirements().",
        ) + '"""SDK names check_telegram_requirements() rebinds in adapter_module after a lazy install."""\n\n'
          + "REBOUND = (\n" + "".join(f'    "{name}",\n' for name in rebound) + ")\n",
    }

    for module, (class_name, purpose) in MIXINS.items():
        body = "".join(chunks[module])
        body, local_rewrites = rewrite_sibling_imports(body)
        # super(TelegramAdapter, self) and TelegramAdapter.<attr> now resolve through the owning mixin.
        renamed = len(re.findall(r"\bTelegramAdapter\b(?![\"'\]])", body))
        body = re.sub(r"super\(\s*TelegramAdapter,", f"super({class_name},", body)
        body = re.sub(r"\bTelegramAdapter\.(\w+)", lambda m: f"{class_name}.{m.group(1)}", body)
        leftover = re.findall(r"\bTelegramAdapter\b(?![\"'\]])", body)
        if leftover:
            raise SystemExit(f"{module}: unhandled TelegramAdapter references: {len(leftover)}")
        if renamed:
            local_rewrites.append(f"{renamed} reference(s) to the TelegramAdapter class -> {class_name}")
        imports = sorted(used_names[module] & module_names)
        import_block = "from .adapter_module import (  # noqa: F401\n" + "".join(
            f"    {name},\n" for name in imports) + ")\n" if imports else ""
        out[f"{module}.py"] = (
            header("adapter.py", local_rewrites, f"TelegramAdapter methods: {purpose}.")
            + import_block + f"\n\nclass {class_name}:\n" + body
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", required=True, type=Path, help="a Hermes git repository that has the pin")
    args = parser.parse_args()
    repo: Path = args.src.resolve()
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"{PIN}^{{commit}}"],
                            capture_output=True, text=True).stdout.strip()
    if commit != PIN:
        raise SystemExit(f"{repo} has no commit {PIN}; run scripts/hermes-src.sh")

    files: dict[str, str] = {}
    for name in SIBLINGS:
        text, rewrites = rewrite_sibling_imports(git_show(repo, f"{SRC_DIR}/{name}.py"))
        text, logger_rewrites = rewrite_logger(text)
        files[f"{name}.py"] = header(f"{name}.py", rewrites + logger_rewrites) + text
    present = {n[:-3] for n in git_ls(repo, SRC_DIR) if n.endswith(".py")} - {"__init__", "adapter"}
    if present != set(SIBLINGS):
        raise SystemExit(f"sibling modules changed upstream: {sorted(present ^ set(SIBLINGS))}")
    files.update(split_adapter(git_show(repo, f"{SRC_DIR}/adapter.py")))
    files["__init__.py"] = header(
        "", [], "Package of harvested code; see scripts/harvest.py for what each module holds.")

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)
    for name, text in sorted(files.items()):
        (OUT_DIR / name).write_text(text, encoding="utf-8")
    (OUT_DIR / "LICENSE.hermes").write_text(git_show(repo, "LICENSE"), encoding="utf-8")
    (OUT_DIR / "PIN").write_text(PIN + "\n", encoding="utf-8")
    total = sum(t.count("\n") for t in files.values())
    print(f"harvested {len(files)} files, {total} lines, from {UPSTREAM}@{PIN[:7]} into {OUT_DIR}")


if __name__ == "__main__":
    sys.exit(main())
