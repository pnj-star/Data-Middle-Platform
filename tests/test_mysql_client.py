from src import mysql_client


def test_scope_cleanup_keeps_only_current_exclusions():
    source_id = "doc-scope-test"
    parent_id = "parent-shared"
    block = {
        "parent_id": parent_id,
        "title": "same section",
        "content": "same content",
        "source_type": "document",
        "source_id": source_id,
    }

    mysql_client.insert_parent_blocks([block], tenant_id="old", kb_id="old-kb")
    mysql_client.insert_parent_blocks([block], tenant_id="new", kb_id="new-kb")

    affected = mysql_client.delete_parents_by_source_scopes(
        source_id,
        [
            {"tenant_id": "old", "kb_id": "old-kb"},
            {"tenant_id": "new", "kb_id": "new-kb"},
        ],
        current_tenant_id="new",
        current_kb_id="new-kb",
        exclude_parent_ids={parent_id},
    )

    assert affected == 1
    assert mysql_client.get_parent_content(
        parent_id, tenant_id="old", kb_id="old-kb"
    ) is None
    assert mysql_client.get_parent_content(
        parent_id, tenant_id="new", kb_id="new-kb"
    )
