"""TC-ISO-01 … TC-ISO-10: the cross-user isolation suite (FR-075 / AL-269).

Each case lists the tests that prove it, as "<file>::<test function>". conftest.py tags
every listed test with `isolation` and `tc_iso("<ID>")`, so the whole suite runs with:

    python -m pytest tests -m isolation

test_isolation_catalog.py fails if a case has no tests or a listed test no longer exists.
Add the tests for new layers here (e.g. keyword/hybrid search in FR-023/024, citations
table in Sprint 3).
"""

CATALOG: dict[str, dict] = {
    "TC-ISO-01": {
        "title": "Private documents are invisible to other users (API and raw SQL)",
        "tests": [
            "test_tc_iso_01_rls.py::test_tc_iso_01_private_document_invisible_to_other_user_via_api_and_sql",
            "test_private_kb_api.py::test_tc_iso_api_02_user_b_direct_access_is_denied",
            "test_rls_per_table.py::test_select_isolation_per_table",
        ],
    },
    "TC-ISO-02": {
        "title": "Other users cannot change, delete, plant or download private documents",
        "tests": [
            "test_document_download.py::test_other_users_private_document_is_404",
            "test_rls_per_table.py::test_other_user_cannot_update_or_delete",
            "test_rls_per_table.py::test_other_user_cannot_insert_rows_owned_by_victim",
            "test_rls_per_table.py::test_admin_cannot_modify_private_rows",
            "test_collections.py::test_cannot_move_someone_elses_public_document",
        ],
    },
    "TC-ISO-03": {
        "title": "Conversation history loads only the caller's conversations and messages",
        "tests": [
            "test_conversation_isolation.py::test_tc_iso_03_conversation_history_is_owner_only",
            "test_conversation_isolation.py::test_unauthenticated_connections_see_no_conversations",
        ],
    },
    "TC-ISO-04": {
        "title": "Citations and messages never cross users",
        "tests": ["test_conversation_isolation.py::test_tc_iso_04_messages_and_citations_cannot_cross_users"],
    },
    "TC-ISO-05": {
        "title": "Chunk authorization discards foreign, stale and mis-stamped chunks",
        "tests": [
            "test_chunk_access_control.py::test_user_b_searches_user_a_private_chunks_denied",
            "test_chunk_access_control.py::test_chunk_metadata_ownership_mismatch_rejected",
            "test_chunk_access_control.py::test_private_chunk_incorrectly_placed_in_public_collection_rejected",
            "test_chunk_access_control.py::test_document_removed_from_postgres_but_chunk_remains_in_chromadb",
            "test_chunk_access_control.py::test_consecutive_requests_from_different_users_no_identity_leakage",
        ],
    },
    "TC-ISO-06": {
        "title": "Files stay in the owner's storage folder and are never served across users",
        "tests": [
            "test_document_download.py::test_stored_path_outside_upload_dir_is_never_served",
            "test_collections.py::test_storage_path_is_inside_the_users_namespace",
            "test_collections.py::test_storage_path_ignores_traversal_in_filenames",
            "test_integrity_check.py::test_detects_missing_file_and_file_outside_owner_folder",
        ],
    },
    "TC-ISO-07": {
        "title": "Vector store: per-user private collections, never opened for another user",
        "tests": [
            "test_private_kb_isolation.py::test_tc_iso_02_search_never_opens_another_users_collection",
            "test_private_kb_isolation.py::test_tc_iso_05_mis_stamped_collection_is_rejected",
            "test_private_kb_isolation.py::test_tc_iso_06_private_filter_requires_owner_and_private_visibility",
            "test_private_kb_isolation.py::test_tc_iso_07_owner_cannot_delete_other_users_chunks",
        ],
    },
    "TC-ISO-08": {
        "title": "Search returns only the caller's private and public results, without owner ids",
        "tests": [
            "test_private_kb_api.py::test_tc_iso_api_01_user_b_search_returns_zero_private_results",
            "test_private_kb_api.py::test_tc_iso_api_03_retrieval_response_hides_owner_ids",
            "test_private_kb_isolation.py::test_tc_iso_01_private_doc_searchable_only_by_owner",
            "test_private_kb_isolation.py::test_tc_iso_04_public_docs_visible_to_all_private_excluded",
        ],
    },
    "TC-ISO-09": {
        "title": "Collections and role data are per user",
        "tests": [
            "test_collections.py::test_other_users_collections_are_invisible",
            "test_collections.py::test_collections_rls_blocks_cross_user_rows",
            "test_collections.py::test_database_rejects_document_in_foreign_collection",
            "test_rls_per_table.py::test_role_assignments_are_read_only_for_the_app",
        ],
    },
    "TC-ISO-10": {
        "title": "Cross-layer integrity: RLS on every table, DB / vectors / files consistent",
        "tests": [
            "test_rls_guard.py::test_every_table_has_forced_rls_and_policies",
            "test_rls_guard.py::test_app_role_cannot_bypass_rls",
            "test_integrity_check.py::test_clean_system_reports_no_issues",
            "test_integrity_check.py::test_detects_private_chunk_in_public_collection",
            "test_integrity_check.py::test_detects_chunk_in_another_users_private_collection",
            "test_private_kb_isolation.py::test_tc_iso_08_deleting_user_removes_only_their_private_collection",
        ],
    },
}


def case_for(file_name: str, function_name: str) -> list[str]:
    """TC-ISO ids that list this test."""
    key = f"{file_name}::{function_name}"
    return [case for case, spec in CATALOG.items() if key in spec["tests"]]
