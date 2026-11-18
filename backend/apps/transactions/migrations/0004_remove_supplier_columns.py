"""Remove the deleted supply-chain module.

The supplier / purchase / inventory / sales apps were deleted, so:

* ``Transaction.supplier`` and ``RecurringTransaction.supplier`` no longer
  exist in the models — the leftover columns are dropped here.
* the old ``inventory_*`` / ``suppliers_*`` / ``purchases_*`` / ``sales_*``
  tables are orphaned; they are dropped too so the database matches the code.

Everything is written to be safe on both fresh databases (where those tables
never existed) and existing ones.
"""

from django.db import migrations

TABLES = (
    ("transactions_transaction", "supplier_id"),
    ("transactions_recurringtransaction", "supplier_id"),
)

# Child tables first so foreign keys never block the drop.
ORPHAN_TABLES = (
    "purchases_purchaseinvoiceitem",
    "purchases_purchasereturnitem",
    "purchases_purchasereturn",
    "purchases_purchaseinvoice",
    "sales_salesinvoiceitem",
    "sales_salereturnitem",
    "sales_salereturn",
    "sales_salesinvoice",
    "inventory_stockmovement",
    "inventory_product",
    "inventory_warehouse",
    "suppliers_supplierledgerentry",
    "suppliers_supplier",
)


def _indexes_for_column(connection, cursor, table, column):
    """Names of indexes on `table` that reference `column`."""
    if connection.vendor == "sqlite":
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = %s AND sql LIKE %s",
            [table, f"%{column}%"])
        return [row[0] for row in cursor.fetchall()]

    try:
        constraints = connection.introspection.get_constraints(cursor, table)
    except Exception:
        return []
    return [name for name, cols in (constraints.get("indexes") or {}).items()
            if column in cols]


def drop_supplier_columns(apps, schema_editor):
    connection = schema_editor.connection
    vendor = connection.vendor
    with connection.cursor() as cursor:
        for table, column in TABLES:
            existing = connection.introspection.get_table_description(
                cursor, table)
            names = {col.name for col in existing}
            if column not in names:
                continue

            # SQLite refuses to drop a column that an index depends on.
            for index_name in _indexes_for_column(
                    connection, cursor, table, column):
                if vendor == "postgresql":
                    schema_editor.execute(
                        f'DROP INDEX IF EXISTS "{index_name}"')
                else:
                    schema_editor.execute(
                        f"DROP INDEX IF EXISTS {index_name}")

            if vendor == "postgresql":
                schema_editor.execute(
                    f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
            else:
                schema_editor.execute(
                    f"ALTER TABLE {table} DROP COLUMN {column}")


def drop_orphan_tables(apps, schema_editor):
    connection = schema_editor.connection
    vendor = connection.vendor
    with connection.cursor() as cursor:
        for table in ORPHAN_TABLES:
            if vendor == "postgresql":
                schema_editor.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
            else:
                schema_editor.execute(f"DROP TABLE IF EXISTS {table}")


def restore_supplier_columns(apps, schema_editor):
    """Reverse is a no-op — the supplier module itself is gone."""
    return None


class Migration(migrations.Migration):

    dependencies = [
        ('transactions', '0003_recurringtransaction'),
    ]

    operations = [
        migrations.RunPython(drop_supplier_columns,
                             restore_supplier_columns),
        migrations.RunPython(drop_orphan_tables, migrations.RunPython.noop),
    ]
