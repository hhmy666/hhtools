"""Central resource limits shared by parsers, planners and JSON Schemas.

The numeric values in ``batch-request.schema.json`` must stay synchronized
with this module.  Cross-field/derived limits (not expressible as a simple
JSON Schema ``maxItems``) are enforced by the Python boundary.
"""

MAX_BATCH_TEMPLATES = 200
MAX_BATCH_PLANS = 50
MAX_TEMPLATE_RULES = 200
MAX_LOCAL_PLANS_PER_TEMPLATE = 50
MAX_PLAN_DEFINITIONS = 200
MAX_RESOLVED_WORK_ITEMS = 1_000


__all__ = [
    "MAX_BATCH_PLANS",
    "MAX_BATCH_TEMPLATES",
    "MAX_LOCAL_PLANS_PER_TEMPLATE",
    "MAX_PLAN_DEFINITIONS",
    "MAX_RESOLVED_WORK_ITEMS",
    "MAX_TEMPLATE_RULES",
]
