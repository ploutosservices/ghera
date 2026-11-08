"""The demo's declaration: what ghera needs to know about demo/schema.sql."""
from ghera import Declaration

DEMO = Declaration(
    tenant_setting='demo.tenant',
    app_setting='demo.application',
    tenant_column='tenant_id',
    deleted_column='is_deleted',
    owners=lambda: {'recipe': 'kitchen', 'plot': 'garden'},
    open_to=lambda: {'plot': ('kitchen',)},
    known=lambda: {'schema_version'},
)
