"""Native migration operations that declare an intended drop of stored data.

Retained history imports these classes, so this module imports only Django.
"""

from __future__ import annotations

from django.db.migrations.operations import DeleteModel, RemoveField


class IntendedRemoveField(RemoveField):
    """Native ``RemoveField`` whose migration intends to drop the column's data.

    Django applies it unchanged. The migrate-time drop guard accepts exactly
    this removal, for example after a preceding ``RunPython`` has moved the data.
    """


class IntendedDeleteModel(DeleteModel):
    """Native ``DeleteModel`` whose migration intends to drop the table's rows.

    Django applies it unchanged. The migrate-time drop guard accepts exactly
    this deletion; other removals in the same migration remain guarded.
    """
