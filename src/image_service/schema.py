"""DynamoDB table definition.

Shared by the deploy script and the test-suite so infrastructure and tests can
never drift apart.

Access patterns:
  1. Get / delete one image by id          -> table primary key (image_id)
  2. List a user's images, newest first,
     optionally within a date range        -> GSI user_id + created_at (Query, no Scan)
  3. Browse all images (admin / explore)   -> paginated Scan (see README trade-offs)
Tag and content-type filters are applied as FilterExpressions on top of 2 or 3.
"""

from __future__ import annotations

from typing import Any, Dict

USER_INDEX_NAME = "user_id-created_at-index"


def table_definition(table_name: str) -> Dict[str, Any]:
    return {
        "TableName": table_name,
        "BillingMode": "PAY_PER_REQUEST",  # on-demand: scales with traffic, no capacity planning
        "AttributeDefinitions": [
            {"AttributeName": "image_id", "AttributeType": "S"},
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        "KeySchema": [{"AttributeName": "image_id", "KeyType": "HASH"}],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": USER_INDEX_NAME,
                "KeySchema": [
                    {"AttributeName": "user_id", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    }
