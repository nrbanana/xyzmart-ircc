"""Pretend supplier API, with a switch to make it fail on purpose.

In Google Cloud it checks an API key that both sides read from Secret Manager,
like a real supplier would.
"""

import os
import uuid

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

API_KEY = os.getenv("SUPPLIER_API_KEY", "")
app = FastAPI(title="Supplier stub")
state = {"down": False}
orders: dict[str, str] = {}  # idempotency_key -> supplier_ref


class SupplierOrder(BaseModel):
    sku: str
    quantity: int
    priority: str
    idempotency_key: str


class Switch(BaseModel):
    down: bool


@app.get("/healthz")
def healthz():
    return {"ok": True, "down": state["down"]}


@app.post("/switch")
def switch(body: Switch):
    state["down"] = body.down
    return state


@app.post("/orders")
def place_order(order: SupplierOrder, x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "Bad API key")
    if state["down"]:
        raise HTTPException(503, "Supplier is down")
    ref = orders.setdefault(order.idempotency_key, "SUP-" + uuid.uuid4().hex[:6].upper())
    return {"supplier_ref": ref, "eta_days": 2 if order.priority == "urgent" else 5}
