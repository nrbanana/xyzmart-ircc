"""Data access for the tool service. Same functions on the laptop and on Google Cloud.

                    laptop (RUN_MODE=local)       Google Cloud (RUN_MODE=gcp)
  stock, orders     SQLite                        Cloud SQL (Postgres), private IP
  loyalty           SQLite view                   BigQuery view (no personal data)
  product search    keywords over catalog.json    Vertex AI Search
"""

import json
import os
import re
import sqlite3
from pathlib import Path

from shared.cloud import RUN_MODE, PROJECT, google_api

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SEED = json.loads((DATA_DIR / "seed_data.json").read_text())
CATALOG = json.loads((DATA_DIR / "catalog.json").read_text())
DB_PATH = os.getenv("DB_PATH", str(DATA_DIR / "ircc.db"))
BQ_VIEW = f"{PROJECT}.{os.getenv('BQ_DATASET', 'ircc_shared')}.loyalty_view"
BQ_LOCATION = os.getenv("BQ_LOCATION", "australia-southeast1")
SEARCH_ENGINE = os.getenv("SEARCH_ENGINE_ID", "ircc-catalog")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS products (sku TEXT PRIMARY KEY, name TEXT, price REAL,
           restock_date TEXT, reorder_quantity INTEGER)""",
    """CREATE TABLE IF NOT EXISTS stock (sku TEXT, warehouse TEXT, quantity INTEGER,
           PRIMARY KEY (sku, warehouse))""",
    """CREATE TABLE IF NOT EXISTS supplier_orders (order_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE,
           sku TEXT, quantity INTEGER, priority TEXT, status TEXT, supplier_ref TEXT, approval_url TEXT,
           created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
    "CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS stockout_events (event_id TEXT PRIMARY KEY, sku TEXT, warehouse TEXT,
           customer_id TEXT, occurred_at TEXT, received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
]
# Laptop only: in Google Cloud these live in BigQuery, created by Terraform.
LOCAL_CUSTOMER_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS customers (customer_id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT,
           email TEXT, card_last4 TEXT, tier TEXT, spend_12m REAL, order_note TEXT)""",
    # The agent only ever reads this view: first name for a friendly message, no surname, email or card.
    """CREATE VIEW IF NOT EXISTS loyalty_view AS
           SELECT customer_id, first_name, tier, spend_12m, order_note FROM customers""",
]


def sql(query: str, **params) -> list[dict]:
    """Run one statement. Both drivers use :name parameters."""
    if RUN_MODE == "gcp":
        import pg8000.native
        con = pg8000.native.Connection(host=os.environ["DB_HOST"], database=os.getenv("DB_NAME", "ircc"),
                                       user=os.getenv("DB_USER", "ircc_app"), password=os.environ["DB_PASSWORD"])
        try:
            rows = con.run(query, **params)
            cols = [c["name"] for c in con.columns or []]
            return [dict(zip(cols, row)) for row in rows or []]
        finally:
            con.close()
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(query, params)]


def init_db(reset: bool = False) -> None:
    """Create tables and load the sample products and stock if empty (or if reset)."""
    for statement in SCHEMA + (LOCAL_CUSTOMER_SCHEMA if RUN_MODE == "local" else []):
        sql(statement)
    if reset:
        for table in ("settings", "stockout_events", "supplier_orders", "stock", "products"):
            sql(f"DELETE FROM {table}")
    if sql("SELECT COUNT(*) AS n FROM products")[0]["n"]:
        return
    for p in SEED["products"]:
        sql("INSERT INTO products VALUES (:sku, :name, :price, :restock_date, :reorder_quantity)",
            sku=p["sku"], name=p["name"], price=p["price"], restock_date=p["restock_date"],
            reorder_quantity=p["reorder_quantity"])
        for warehouse, qty in p["stock"].items():
            sql("INSERT INTO stock VALUES (:sku, :w, :q)", sku=p["sku"], w=warehouse, q=qty)
    if RUN_MODE == "local":
        for c in SEED["customers"]:
            sql("INSERT OR REPLACE INTO customers VALUES (:customer_id, :first_name, :last_name, :email,"
                " :card_last4, :tier, :spend_12m, :order_note)", **c)


# ---- stock -----------------------------------------------------------------

def get_product(sku: str) -> dict | None:
    rows = sql("SELECT * FROM products WHERE sku = :sku", sku=sku)
    if not rows:
        return None
    product = rows[0]
    product["warehouses"] = sql("SELECT warehouse, quantity FROM stock WHERE sku = :sku ORDER BY warehouse", sku=sku)
    product["available"] = sum(w["quantity"] for w in product["warehouses"])
    return product


def take_stock(sku: str, warehouse: str, quantity: int) -> bool:
    """Remove stock for a sale in one atomic statement. False if there was not enough."""
    return bool(sql("UPDATE stock SET quantity = quantity - :q WHERE sku = :sku AND warehouse = :w"
                    " AND quantity >= :q RETURNING quantity", q=quantity, sku=sku, w=warehouse))


def save_stockout(event: dict) -> bool:
    """Record a stock-out event in one atomic statement. False if this event id was already recorded."""
    return bool(sql("INSERT INTO stockout_events (event_id, sku, warehouse, customer_id, occurred_at)"
                    " VALUES (:event_id, :sku, :warehouse, :customer_id, :occurred_at)"
                    " ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                    **event | {"occurred_at": event["occurred_at"].isoformat()}))


def get_setting(name: str) -> str:
    rows = sql("SELECT value FROM settings WHERE name = :name", name=name)
    return rows[0]["value"] if rows else ""


def set_setting(name: str, value: str) -> None:
    sql("INSERT INTO settings (name, value) VALUES (:name, :value)"
        " ON CONFLICT (name) DO UPDATE SET value = excluded.value", name=name, value=value)


# ---- supplier orders -------------------------------------------------------

def find_order(idempotency_key: str) -> dict | None:
    rows = sql("SELECT * FROM supplier_orders WHERE idempotency_key = :k", k=idempotency_key)
    return rows[0] if rows else None


def get_order(order_id: str) -> dict | None:
    rows = sql("SELECT * FROM supplier_orders WHERE order_id = :id", id=order_id)
    return rows[0] if rows else None


def save_order(order: dict) -> None:
    sql("INSERT INTO supplier_orders (order_id, idempotency_key, sku, quantity, priority, status, supplier_ref)"
        " VALUES (:order_id, :idempotency_key, :sku, :quantity, :priority, :status, :supplier_ref)",
        **{k: order.get(k) for k in ("order_id", "idempotency_key", "sku", "quantity", "priority",
                                      "status", "supplier_ref")})


def update_order(order_id: str, status: str, supplier_ref: str | None = None, approval_url: str | None = None) -> None:
    sql("UPDATE supplier_orders SET status = :status, supplier_ref = COALESCE(:ref, supplier_ref),"
        " approval_url = COALESCE(:url, approval_url) WHERE order_id = :id",
        status=status, ref=supplier_ref, url=approval_url, id=order_id)


def open_orders() -> list[dict]:
    """Orders waiting for a person (large orders) or for the supplier to come back."""
    return sql("SELECT * FROM supplier_orders WHERE status IN ('PENDING_APPROVAL', 'QUEUED') ORDER BY created_at")


# ---- loyalty (BigQuery in the cloud) ---------------------------------------

def _bigquery(query: str, **params) -> list[dict]:
    body = google_api("POST", f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/queries", json={
        "query": query, "useLegacySql": False, "location": BQ_LOCATION, "parameterMode": "NAMED",
        "queryParameters": [{"name": k, "parameterType": {"type": "STRING"}, "parameterValue": {"value": v}}
                            for k, v in params.items()]})
    names = [f["name"] for f in body["schema"]["fields"]]
    rows = [dict(zip(names, (cell["v"] for cell in row["f"]))) for row in body.get("rows", [])]
    for row in rows:
        row["spend_12m"] = float(row["spend_12m"])
    return rows


def get_loyalty(customer_id: str) -> dict | None:
    if RUN_MODE == "gcp":
        rows = _bigquery(f"SELECT * FROM `{BQ_VIEW}` WHERE customer_id = @id", id=customer_id)
    else:
        rows = sql("SELECT * FROM loyalty_view WHERE customer_id = :id", id=customer_id)
    return rows[0] if rows else None


def list_loyalty() -> list[dict]:
    if RUN_MODE == "gcp":
        return _bigquery(f"SELECT * FROM `{BQ_VIEW}` ORDER BY customer_id")
    return sql("SELECT * FROM loyalty_view ORDER BY customer_id")


# ---- product search (Vertex AI Search in the cloud) ------------------------

def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def _search_skus(query: str) -> list[str]:
    """Catalogue matches, best first, ignoring stock."""
    if RUN_MODE == "gcp":
        body = google_api("POST", f"https://discoveryengine.googleapis.com/v1/projects/{PROJECT}/locations/global"
                                  f"/collections/default_collection/engines/{SEARCH_ENGINE}"
                                  "/servingConfigs/default_search:search", json={"query": query, "pageSize": 10})
        return [r["document"]["id"] for r in body.get("results", [])]
    wanted = _words(query)
    names = {p["sku"]: p["name"] for p in SEED["products"]}
    scored = [(len(wanted & _words(f"{names[i['sku']]} {i['category']} {i['description']}")), i["sku"])
              for i in CATALOG]
    return [sku for score, sku in sorted(scored, key=lambda s: -s[0]) if score]


def search_products(query: str, exclude_sku: str = "", limit: int = 3) -> list[dict]:
    """Similar products that are in stock right now (search finds them, Cloud SQL confirms stock)."""
    results = []
    for sku in _search_skus(query):
        product = get_product(sku) if sku != exclude_sku else None
        if product and product["available"] > 0:
            results.append({"sku": sku, "name": product["name"], "price": product["price"],
                            "available": product["available"]})
    return results[:limit]


def demo_data() -> dict:
    stock = sql("SELECT p.sku, p.name, s.warehouse, s.quantity, p.restock_date"
                " FROM products p JOIN stock s ON s.sku = p.sku ORDER BY p.sku, s.warehouse")
    return {"stock": stock, "customers": list_loyalty(), "catalog": CATALOG}
