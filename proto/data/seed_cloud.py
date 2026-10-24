"""Loads the sample customers into BigQuery and the catalogue into Vertex AI Search.

Run by `make deploy` with your own gcloud login. Cloud SQL is not seeded here: it has a
private address only, so the tool service seeds it on first start.

Usage: python seed_cloud.py PROJECT_ID REGION
"""

import json
import sys
import time
from pathlib import Path

import google.auth
from google.auth.transport.requests import AuthorizedSession

HERE = Path(__file__).parent
SEED = json.loads((HERE / "seed_data.json").read_text())
CATALOG = json.loads((HERE / "catalog.json").read_text())
SEARCH = "https://discoveryengine.googleapis.com/v1/projects/{p}/locations/global/collections/default_collection"

# Which catalogue fields Vertex AI Search should match on.
SEARCH_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "name": {"type": "string", "keyPropertyMapping": "title", "retrievable": True},
        "description": {"type": "string", "keyPropertyMapping": "description", "retrievable": True},
        "category": {"type": "string", "retrievable": True, "searchable": True, "indexable": True},
    },
}


def wait(session: AuthorizedSession, operation: dict, base: str) -> None:
    while not operation.get("done"):
        time.sleep(3)
        operation = session.get(f"https://discoveryengine.googleapis.com/v1/{operation['name']}").json()
    if "error" in operation:
        sys.exit(f"Vertex AI Search operation failed: {operation['error']}")


def main(project: str, region: str) -> None:
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)

    rows = ", ".join(
        f"('{c['customer_id']}', '{c['first_name']}', '{c['last_name']}', '{c['email']}', '{c['card_last4']}',"
        f" '{c['tier']}', {c['spend_12m']}, {json.dumps(c['order_note'])})" for c in SEED["customers"])
    query = (f"TRUNCATE TABLE `{project}.ircc_private.customers`;\n"
             f"INSERT INTO `{project}.ircc_private.customers` VALUES {rows};")
    r = session.post(f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries",
                     json={"query": query, "useLegacySql": False, "location": region, "timeoutMs": 60000})
    r.raise_for_status()
    print(f"BigQuery: loaded {len(SEED['customers'])} customers")

    store = SEARCH.format(p=project) + "/dataStores/ircc-catalog"
    r = session.patch(f"{store}/schemas/default_schema", json={"jsonSchema": json.dumps(SEARCH_SCHEMA)})
    r.raise_for_status()
    wait(session, r.json(), store)
    names = {p["sku"]: p["name"] for p in SEED["products"]}
    documents = [{"id": item["sku"], "structData": {"name": names[item["sku"]], "category": item["category"],
                                                   "description": item["description"]}} for item in CATALOG]
    r = session.post(f"{store}/branches/default_branch/documents:import",
                     json={"inlineSource": {"documents": documents}, "reconciliationMode": "INCREMENTAL"})
    r.raise_for_status()
    wait(session, r.json(), store)
    print(f"Vertex AI Search: imported {len(documents)} products (searchable in a few minutes)")


if __name__ == "__main__":
    main(*sys.argv[1:3])
