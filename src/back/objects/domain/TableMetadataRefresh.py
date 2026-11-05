"""Re-read one data-source table from Unity Catalog into its metadata entry."""

from __future__ import annotations

from typing import Any, Dict


class TableMetadataRefresh:
    """Shared per-table step of the sync and async metadata refresh paths."""

    @staticmethod
    def refresh(
        client: Any, catalog: str, schema: str, table_name: str, old_table: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Merge fresh UC columns / comment / SELECT probe into *old_table* in place.

        Returns the column diff, or ``{}`` when the schema did not change.
        Errors propagate so the caller can record them per table.
        """
        from back.objects.domain.Domain import (
            compute_column_diff,
            has_column_changes,
            merge_table_metadata,
        )

        kind = old_table.get("object_kind") or "table"
        if kind == "metric_view":
            new_columns = client.get_metric_view_columns_with_roles(catalog, schema, table_name)
        else:
            new_columns = client.get_table_columns(catalog, schema, table_name)
        table_comment = client.get_table_comment(catalog, schema, table_name)
        select_probe = client.check_table_select_permission(
            catalog, schema, table_name, object_kind=kind, columns=new_columns
        )
        # Snapshot before the merge -- merge_table_metadata replaces
        # old_table["columns"] in place, so the previous schema is lost afterwards.
        table_diff = compute_column_diff(old_table.get("columns", []), new_columns)
        merge_table_metadata(
            old_table,
            new_columns,
            table_comment,
            catalog,
            schema,
            table_name,
            select_probe=select_probe,
        )
        return table_diff if has_column_changes(table_diff) else {}
