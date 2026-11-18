"""Superseded by the removal of the supplier module.

The supplier/purchase/inventory/sales supply-chain apps were deleted, so the
`supplier` FK is gone. This migration is kept (name and sequence) so existing
databases keep a consistent migration history — it now does nothing.

The actual column removal lives in ``0004_remove_supplier_columns``.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('transactions', '0001_initial'),
    ]

    operations = []
