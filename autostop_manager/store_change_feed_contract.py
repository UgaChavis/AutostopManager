"""Digest-only Store references; these do not grant search, detail or write access.

Mirrors Store's agent_change_feed_registry and compact serializer. Keep this
allowlist separate from the business entities supported by the Agent API.
"""

from __future__ import annotations


# entity -> (owner source table, primary-key field)
STORE_CHANGE_FEED_SOURCES: dict[str, tuple[str, str]] = {
    "store_admin_user": ("admin_users", "id"),
    "store_app_setting": ("app_settings", "key"),
    "store_audit_log": ("audit_log", "id"),
    "store_blocked_buyer": ("blocked_buyers", "id"),
    "store_cart_item": ("cart_items", "id"),
    "store_category": ("categories", "id"),
    "store_category_marketplace_mapping": ("category_marketplace_mappings", "id"),
    "store_customer_notification": ("customer_notifications", "id"),
    "store_customer": ("customers", "id"),
    "store_favorite_item": ("favorite_items", "id"),
    "store_manufacturer": ("manufacturers", "id"),
    "store_marketplace_account": ("marketplace_accounts", "id"),
    "store_marketplace_export_job": ("marketplace_export_jobs", "id"),
    "store_marketplace_listing": ("marketplace_listings", "id"),
    "store_marketplace_publication_policy": ("marketplace_publication_policies", "id"),
    "store_part_fitment": ("part_avito_fitments", "id"),
    "store_part_crossreference": ("part_crossreferences", "id"),
    "store_part_photo": ("part_photos", "id"),
    "store_part": ("parts", "id"),
    "store_quote_offer": ("quote_offers", "id"),
    "store_quote_request_item": ("quote_request_items", "id"),
    "store_quote_request_note": ("quote_request_notes", "id"),
    "store_quote_request_vin_photo": ("quote_request_vin_photos", "id"),
    "store_quote_request": ("quote_requests", "id"),
    "store_order_item": ("sales_order_items", "id"),
    "store_order": ("sales_orders", "id"),
    "store_service_principal_credential": ("service_principal_credentials", "id"),
    "store_service_principal": ("service_principals", "id"),
    "store_batch": ("stock_batches", "id"),
    "store_stock_movement": ("stock_movements", "id"),
    "store_warehouse_operation_item": ("warehouse_operation_items", "id"),
    "store_warehouse_operation": ("warehouse_operations", "id"),
}
STORE_CHANGE_FEED_ENTITY_TYPES = frozenset((*STORE_CHANGE_FEED_SOURCES, "store_state"))
STORE_CHANGE_FEED_COMPATIBILITY_ENTITIES = frozenset(
    {
        "store_part",
        "store_order",
        "store_quote_request",
        "store_batch",
        "store_warehouse_operation",
        "store_marketplace_listing",
        "store_state",
    }
)
STORE_CHANGE_KINDS = frozenset(
    {
        "created",
        "updated",
        "deleted",
        "archived",
        "restored",
        "status_changed",
        "activated",
        "deactivated",
        "revoked",
        "related_updated",
    }
)
STORE_CHANGE_FEED_RELATED_ENTITIES: dict[str, tuple[str, ...]] = {
    "store_cart_item": ("store_customer", "store_part"),
    "store_category_marketplace_mapping": ("store_category", "store_marketplace_account"),
    "store_customer_notification": ("store_customer", "store_order"),
    "store_favorite_item": ("store_customer", "store_part"),
    "store_marketplace_export_job": ("store_marketplace_account", "store_part"),
    "store_marketplace_listing": ("store_marketplace_account", "store_part"),
    "store_marketplace_publication_policy": ("store_marketplace_account", "store_part"),
    "store_part_fitment": ("store_part",),
    "store_part_crossreference": ("store_part", "store_part"),
    "store_part_photo": ("store_part",),
    "store_quote_offer": ("store_quote_request_item",),
    "store_quote_request_item": ("store_quote_request",),
    "store_quote_request_note": ("store_quote_request",),
    "store_quote_request_vin_photo": ("store_quote_request",),
    "store_order_item": ("store_order", "store_part"),
    "store_order": ("store_customer",),
    "store_service_principal_credential": ("store_service_principal",),
    "store_batch": ("store_part",),
    "store_stock_movement": ("store_part", "store_batch"),
    "store_warehouse_operation_item": ("store_warehouse_operation", "store_part"),
    "store_warehouse_operation": ("store_order",),
}
STORE_CHANGE_FEED_SAFE_REF_ACCESS = {
    "store_audit_log": "owner_filtered_audit_log",
    "store_service_principal": "security_provisioning_audit_only",
    "store_service_principal_credential": "security_provisioning_audit_only",
}
