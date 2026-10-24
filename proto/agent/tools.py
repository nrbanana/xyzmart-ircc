"""The agent's four tools. Each one is a thin HTTP call to the tool service.

ADK reads each function's name, arguments and docstring and describes them to
Gemini, which decides which to call and in what order. The agent never touches
data directly.
"""

import asyncio
import os

import httpx
from google.adk.tools import ToolContext

from shared.cloud import auth_headers

TOOL_SERVICE_URL = os.getenv("TOOL_SERVICE_URL", "http://localhost:8081")
TRANSPORT = None  # tests swap in an in-process transport


async def call_tool_service(method: str, path: str, **kwargs) -> dict:
    """Call the tool service, retrying twice with backoff. Never raises."""
    error = ""
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(base_url=TOOL_SERVICE_URL, transport=TRANSPORT, timeout=10,
                                         headers=auth_headers(TOOL_SERVICE_URL)) as client:
                r = await client.request(method, path, **kwargs)
            if r.status_code < 500:
                return r.json() if r.is_success else {"error": r.json().get("detail", r.text)}
            error = f"tool service returned {r.status_code}"
        except httpx.HTTPError as e:
            error = f"tool service unreachable: {e.__class__.__name__}"
        await asyncio.sleep(0.3 * 2**attempt)
    return {"error": error}


async def check_stock(sku: str) -> dict:
    """Live stock for a product: units per warehouse, restock date and suggested reorder quantity.

    Args:
        sku: Product code, for example "HP-100".
    """
    return await call_tool_service("GET", f"/stock/{sku}")


async def find_similar_products(query: str, exclude_sku: str) -> dict:
    """Find in-stock products similar to a description. Only returns products that are in stock.

    Args:
        query: Words describing the product, for example its name or category.
        exclude_sku: The sold-out product code, so it is not returned.
    """
    return await call_tool_service("GET", "/search", params={"q": query, "exclude_sku": exclude_sku})


async def check_loyalty(customer_id: str) -> dict:
    """Customer loyalty tier (Gold, Silver or Bronze), 12 month spend and order note. No personal data.

    Args:
        customer_id: Customer code, for example "C-1001".
    """
    return await call_tool_service("GET", f"/loyalty/{customer_id}")


async def submit_order(sku: str, quantity: int, priority: str, idempotency_key: str) -> dict:
    return await call_tool_service("POST", "/supplier-orders", json={
        "sku": sku, "quantity": quantity, "priority": priority, "idempotency_key": idempotency_key})


async def order_from_supplier(sku: str, quantity: int, priority: str, tool_context: ToolContext) -> dict:
    """Order more stock from the supplier. Returns an order id and status.

    Status is ORDERED, PENDING_APPROVAL (large orders wait for a person) or
    QUEUED (supplier is down; it will be sent later).

    Args:
        sku: Product code to reorder.
        quantity: Units to order. Use the suggested reorder quantity from check_stock.
        priority: "urgent" for Gold customers, otherwise "normal".
    """
    # One order per alert and product, however many times the model or a retry calls this.
    key = f"{tool_context.state['alert_id']}:{sku}"
    return await submit_order(sku, quantity, priority, key)
