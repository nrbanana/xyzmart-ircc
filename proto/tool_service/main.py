"""Tool service: the only part of the system that reads data or places orders.

It does three jobs:
  1. Records each stock-out event, so a message Pub/Sub delivers twice is acted on once.
  2. Serves the agent's tools: stock, loyalty, similar products, supplier order.
  3. Enforces the business rules in code, so a tricked or faulty model cannot break them:
     - maximum discount and allowed offer types per loyalty tier
     - only in-stock products may be offered as substitutes
     - supplier orders above APPROVAL_THRESHOLD wait for a person
     - each supplier order has an idempotency key, so retries never double-order
"""

import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import store
from shared import cloud

SUPPLIER_URL = os.getenv("SUPPLIER_URL", "http://localhost:8082")
SUPPLIER_API_KEY = os.getenv("SUPPLIER_API_KEY", "")  # from Secret Manager in Google Cloud
WORKFLOW = os.getenv("SUPPLIER_WORKFLOW", "")  # projects/.../locations/.../workflows/supplier-order
APPROVAL_THRESHOLD = 50
DISCOUNT_CAP = {"Gold": 15, "Silver": 10, "Bronze": 5}
ALLOWED_OFFERS = {
    "Gold": {"priority_reservation", "substitute", "back_in_stock_alert"},
    "Silver": {"substitute", "back_in_stock_alert"},
    "Bronze": {"substitute", "back_in_stock_alert"},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()  # Cloud SQL is private, so the service seeds itself on first start
    yield


app = FastAPI(title="xyzMart tool service", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---- 1. stock-out events: each one is handled once -------------------------

class StockoutEvent(BaseModel):
    """A purchase found no stock. The contract is written out in the README, "Stock-out event contract"."""
    event_id: str = Field(min_length=1, max_length=64)
    sku: str = Field(min_length=1)
    warehouse: Literal["SYD", "MEL"]
    customer_id: str = Field(min_length=1)
    occurred_at: datetime


@app.post("/stockout-events")
def record_stockout(event: StockoutEvent):
    """The agent service calls this before acting. Pub/Sub can deliver twice; only the first counts."""
    return {"duplicate": not store.save_stockout(event.model_dump())}


class Purchase(BaseModel):
    sku: str
    warehouse: Literal["SYD", "MEL"]


@app.post("/demo/purchase")
def demo_purchase(body: Purchase):
    """Demo only: stands in for xyzMart's order system, which owns stock in production.
    Takes one unit in a single atomic statement, so two buyers cannot both get the last one."""
    if not store.get_product(body.sku):
        raise HTTPException(404, f"Unknown product {body.sku}")
    return {"result": "sold" if store.take_stock(body.sku, body.warehouse, 1) else "out_of_stock"}


@app.post("/admin/reset")
def reset_demo():
    """Put stock back to the sample values and clear orders. Demo only."""
    store.init_db(reset=True)
    return {"ok": True}


# ---- 2. the agent's tools --------------------------------------------------

@app.get("/stock/{sku}")
def check_stock(sku: str):
    product = store.get_product(sku)
    if not product:
        raise HTTPException(404, f"Unknown product {sku}")
    return {
        "sku": sku,
        "name": product["name"],
        "available": product["available"],
        "warehouses": product["warehouses"],
        "restock_date": product["restock_date"],
        "suggested_reorder_quantity": product["reorder_quantity"],
    }


@app.get("/loyalty/{customer_id}")
def check_loyalty(customer_id: str):
    loyalty = store.get_loyalty(customer_id)
    if not loyalty:
        raise HTTPException(404, f"Unknown customer {customer_id}")
    return loyalty


@app.get("/search")
def find_similar_products(q: str, exclude_sku: str = ""):
    return {"results": store.search_products(q, exclude_sku)}


class OrderRequest(BaseModel):
    sku: str
    quantity: int = Field(gt=0, le=1000)
    priority: Literal["normal", "urgent"] = "normal"
    idempotency_key: str


def _send_to_supplier(order: dict) -> tuple[str, str | None]:
    """Laptop path: try the supplier three times. Returns (status, supplier_ref)."""
    for attempt in range(3):
        try:
            r = cloud_call("POST", f"{SUPPLIER_URL}/orders", json={
                "sku": order["sku"], "quantity": order["quantity"],
                "priority": order["priority"], "idempotency_key": order["idempotency_key"]})
            r.raise_for_status()
            return "ORDERED", r.json()["supplier_ref"]
        except httpx.HTTPError:
            time.sleep(0.2 * 2**attempt)
    return "QUEUED", None


def cloud_call(method: str, url: str, **kwargs) -> httpx.Response:
    headers = cloud.auth_headers(url) | ({"X-Api-Key": SUPPLIER_API_KEY} if SUPPLIER_API_KEY else {})
    return httpx.request(method, url, headers=headers, timeout=5, **kwargs)


def _start_workflow(order: dict, needs_approval: bool) -> None:
    """Google Cloud path: Workflows waits for approval if needed, then calls the supplier with retries."""
    cloud.google_api("POST", f"https://workflowexecutions.googleapis.com/v1/{WORKFLOW}/executions", json={
        "argument": json.dumps({k: order[k] for k in ("order_id", "sku", "quantity", "priority", "idempotency_key")}
                               | {"needs_approval": needs_approval})})


@app.post("/supplier-orders")
def order_from_supplier(req: OrderRequest):
    existing = store.find_order(req.idempotency_key)
    if existing:
        return {"order_id": existing["order_id"], "status": existing["status"], "duplicate": True}
    if not store.get_product(req.sku):
        raise HTTPException(404, f"Unknown product {req.sku}")
    order = req.model_dump() | {"order_id": "PO-" + uuid.uuid4().hex[:8].upper(), "supplier_ref": None}
    needs_approval = req.quantity > APPROVAL_THRESHOLD
    if WORKFLOW:
        order["status"] = "PENDING_APPROVAL" if needs_approval else "SUBMITTED"
        store.save_order(order)
        _start_workflow(order, needs_approval)
    else:
        order["status"] = "PENDING_APPROVAL"
        if not needs_approval:
            order["status"], order["supplier_ref"] = _send_to_supplier(order)
        store.save_order(order)
    return {"order_id": order["order_id"], "status": order["status"]}


@app.get("/supplier-orders")
def list_orders():
    return {"open": store.open_orders()}


@app.post("/supplier-orders/{order_id}/send")
def send_order(order_id: str):
    """Approve a large order, or retry one queued while the supplier was down."""
    order = store.get_order(order_id)
    if not order or order["status"] not in ("PENDING_APPROVAL", "QUEUED"):
        raise HTTPException(409, "Order is not waiting")
    if WORKFLOW and order["status"] == "PENDING_APPROVAL":
        if not order["approval_url"]:
            raise HTTPException(409, "Workflow is not waiting yet, try again in a moment")
        cloud.google_api("POST", order["approval_url"], json={"approved": True})
        status, ref = "SUBMITTED", None
    elif WORKFLOW:
        _start_workflow(order, needs_approval=False)
        status, ref = "SUBMITTED", None
    else:
        status, ref = _send_to_supplier(order)
    store.update_order(order_id, status, ref)
    return {"order_id": order_id, "status": status}


class OrderStatus(BaseModel):
    status: Literal["PENDING_APPROVAL", "ORDERED", "QUEUED"]
    supplier_ref: str | None = None
    approval_url: str | None = None


@app.post("/supplier-orders/{order_id}/status")
def set_order_status(order_id: str, body: OrderStatus):
    """Called by the Workflows execution as the order moves along."""
    store.update_order(order_id, body.status, body.supplier_ref, body.approval_url)
    return {"ok": True}


# ---- 3. final gate on every offer ------------------------------------------

class Offer(BaseModel):
    sold_out: bool
    offer_type: Literal["priority_reservation", "substitute", "back_in_stock_alert", "ship_from_other_warehouse"]
    sku: str
    discount_percent: int
    message_to_customer: str
    supplier_order_id: str | None = None


class OfferCheck(BaseModel):
    customer_id: str
    sold_out_sku: str
    offer: Offer


@app.post("/offers/check")
def check_offer(req: OfferCheck):
    """Final gate before an offer reaches a customer. Fixes anything outside the rules."""
    loyalty = store.get_loyalty(req.customer_id)
    product = store.get_product(req.sold_out_sku)
    if not loyalty or not product:
        raise HTTPException(404, "Unknown customer or product")
    tier, offer, problems = loyalty["tier"], req.offer.model_copy(), []

    really_sold_out = product["available"] == 0
    if offer.sold_out != really_sold_out:
        problems.append(f"classified as {'sold out' if offer.sold_out else 'in stock'}, but stock is "
                        f"{product['available']}")
        offer.sold_out = really_sold_out
    if not really_sold_out:
        if offer.offer_type != "ship_from_other_warehouse" or offer.discount_percent:
            problems.append("product is in stock elsewhere: ship it, no discount")
        offer.offer_type, offer.discount_percent = "ship_from_other_warehouse", 0
    elif offer.offer_type == "ship_from_other_warehouse":
        problems.append("nothing left in any warehouse to ship")
        offer.offer_type = "back_in_stock_alert"
    if offer.offer_type not in ALLOWED_OFFERS[tier] | {"ship_from_other_warehouse"}:
        problems.append(f"{offer.offer_type} is not allowed for {tier}")
        offer.offer_type = "back_in_stock_alert"
    if offer.offer_type == "substitute":
        substitute = store.get_product(offer.sku)
        if offer.sku == req.sold_out_sku or not substitute or substitute["available"] <= 0:
            problems.append(f"substitute {offer.sku} is not in stock")
            offer.offer_type = "back_in_stock_alert"
    if offer.offer_type != "substitute":
        offer.sku = req.sold_out_sku
    cap = DISCOUNT_CAP[tier]
    if not 0 <= offer.discount_percent <= cap:
        problems.append(f"discount {offer.discount_percent}% is outside 0-{cap}% for {tier}")
        offer.discount_percent = max(0, min(offer.discount_percent, cap))

    if problems:
        # The model's wording may repeat the bad promise, so replace it with a safe template.
        name = store.get_product(offer.sku)["name"]
        offer.message_to_customer = f"Hi {loyalty['first_name']}, " + {
            "priority_reservation": f"we have reserved the next {name} for you",
            "substitute": f"{name} is in stock now as an alternative",
            "back_in_stock_alert": f"we will tell you as soon as {name} is back in stock",
            "ship_from_other_warehouse": f"good news: {name} is in stock at another warehouse and will ship from there",
        }[offer.offer_type] + (f", with {offer.discount_percent}% off." if offer.discount_percent else ".")
    return {"approved": not problems, "problems": problems, "tier": tier,
            "discount_cap": cap, "offer": offer.model_dump()}


@app.get("/demo-data")
def demo_data():
    return store.demo_data()
