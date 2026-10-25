"""Agent service: receives stock-out events, runs the agent, sends offers, serves the demo screen.

Message flow (Pub/Sub in Google Cloud, direct HTTP on the laptop):
  web shop (here: the demo screen) -> stockout-events -> this service, /pubsub/stockouts (runs the agent)
  this service                     -> customer-offers -> customer channels (here: the demo screen)
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE.parent / ".env")  # before importing the agent, which reads the model name

import httpx  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402
from pydantic import BaseModel, Field, ValidationError  # noqa: E402

import agent  # noqa: E402
import rules  # noqa: E402
import tools  # noqa: E402
from shared import cloud  # noqa: E402

SUPPLIER_URL = os.getenv("SUPPLIER_URL", "http://localhost:8082")
AGENT_TIMEOUT_SECONDS = 60

sessions = InMemorySessionService()
runner = Runner(agent=agent.root_agent, app_name="ircc", session_service=sessions)
offers: list[dict] = []
runs: dict[str, dict] = {}  # event_id -> latest delivery of that event, shown on the demo screen
switches = {"ai_off": False, "supplier_down": False}


def required_tools(offer: dict) -> set[str]:
    """An offer is only trusted if it is grounded in these tool results."""
    return {"check_stock", "check_loyalty"} | ({"order_from_supplier"} if offer["sold_out"] else set())


async def run_agent(stockout: dict, run: dict) -> agent.Offer:
    """One agent run. Each stock-out event gets its own session: its working memory."""
    session = await sessions.create_session(app_name="ircc", user_id="ircc",
                                            state={"event_id": stockout["event_id"]})
    message = types.Content(role="user", parts=[types.Part(text=json.dumps(stockout))])
    open_calls = {}
    async for event in runner.run_async(user_id="ircc", session_id=session.id, new_message=message):
        if event.usage_metadata and event.usage_metadata.total_token_count:
            run["tokens"] += event.usage_metadata.total_token_count
        for call in event.get_function_calls():
            if call.name != "set_model_response":
                open_calls[call.id] = {"tool": call.name, "input": call.args, "output": None}
                run["steps"].append(open_calls[call.id])
        for response in event.get_function_responses():
            if response.id in open_calls:
                open_calls[response.id]["output"] = response.response
    session = await sessions.get_session(app_name="ircc", user_id="ircc", session_id=session.id)
    result = session.state.get("offer")
    if result is None:
        raise ValueError("agent returned no offer")
    return agent.Offer.model_validate_json(result) if isinstance(result, str) else agent.Offer.model_validate(result)


async def handle_stockout(stockout: dict, run: dict) -> None:
    record = await tools.call_tool_service("POST", "/stockout-events", json=stockout)
    if "error" in record:
        run.update(status="error", note=f"Could not record the event: {record['error']}")
        return
    if record["duplicate"]:
        run.update(status="skipped", note="Duplicate event id, already handled. No second order.")
        return
    run["status"] = "running"
    try:
        if switches["ai_off"]:
            raise RuntimeError("AI switched off")
        offer = (await asyncio.wait_for(run_agent(stockout, run), AGENT_TIMEOUT_SECONDS)).model_dump()
        skipped = required_tools(offer) - {step["tool"] for step in run["steps"]}
        if skipped:
            raise ValueError(f"agent skipped {', '.join(sorted(skipped))}")
        run["source"] = "ai"
    except Exception as e:  # Gemini down, slow, or invalid output: fall back to fixed rules
        run.update(source="rules", note=f"Rules fallback used: {str(e) or type(e).__name__}")  # a timeout has no message
        offer = await rules.decide(stockout, log=lambda tool, args, result: run["steps"].append(
            {"tool": tool, "input": args, "output": result}))

    run["proposed_offer"] = offer
    check = await tools.call_tool_service("POST", "/offers/check", json={
        "customer_id": stockout["customer_id"], "sold_out_sku": stockout["sku"], "offer": offer})
    if "error" in check:
        run.update(status="error", note=f"Offer check failed: {check['error']}")
        return
    run.update(check=check, status="done")
    sent = {"event_id": stockout["event_id"], "customer_id": stockout["customer_id"], **check["offer"]}
    offers.append(sent)
    cloud.publish("customer-offers", sent)


app = FastAPI(title="xyzMart recovery agent")


@app.get("/")
def demo_screen():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


class StockoutEvent(BaseModel):
    """Checked here so a bad message is dropped before it reaches the model. Contract: README, "Stock-out event"."""
    event_id: str = Field(min_length=1, max_length=64)
    sku: str = Field(min_length=1)
    warehouse: Literal["SYD", "MEL"]
    customer_id: str = Field(min_length=1)
    occurred_at: datetime


@app.post("/pubsub/stockouts")
async def on_stockout(envelope: dict):
    """Pub/Sub push endpoint for stock-out events. Always answers 2xx, so Pub/Sub stops retrying."""
    try:
        stockout = StockoutEvent(**cloud.read_push(envelope)).model_dump(mode="json")
    except (ValidationError, KeyError, ValueError, TypeError) as e:
        return {"status": "ignored", "reason": str(e)[:200]}
    run = {"event": stockout, "status": "received", "steps": [], "tokens": 0, "source": None, "note": None}
    runs[stockout["event_id"]] = run
    try:
        await handle_stockout(stockout, run)
    except Exception as e:
        run.update(status="error", note=str(e)[:300])
    return {"status": run["status"]}


# ---- demo screen API -------------------------------------------------------

class Purchase(BaseModel):
    customer_id: str
    sku: str
    warehouse: str


@app.post("/api/purchase")
async def purchase(body: Purchase):
    """The demo screen plays the web shop. If the purchase finds no stock, it publishes a stock-out event,
    as xyzMart's web shop would in production."""
    sale = await tools.call_tool_service("POST", "/demo/purchase", json={"sku": body.sku, "warehouse": body.warehouse})
    if sale.get("result") != "out_of_stock":
        return sale
    stockout = {"event_id": "WEB-" + uuid.uuid4().hex[:8].upper(), **body.model_dump(),
                "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    cloud.publish("stockout-events", stockout)
    return sale | {"event": stockout}


@app.get("/api/runs/{event_id}")
def get_run(event_id: str):
    if event_id not in runs:
        raise HTTPException(404, "Event not received yet")
    return runs[event_id]


@app.post("/api/stockouts/{event_id}/redeliver")
def redeliver(event_id: str):
    """Publish the same stock-out event again, as Pub/Sub sometimes does."""
    if event_id not in runs:
        raise HTTPException(404)
    cloud.publish("stockout-events", runs[event_id]["event"])
    return {"ok": True}


@app.get("/api/offers")
def list_offers():
    return {"offers": offers[::-1]}


@app.get("/api/demo-data")
async def demo_data():
    return await tools.call_tool_service("GET", "/demo-data")


@app.post("/api/reset")
async def reset():
    offers.clear()
    runs.clear()
    return await tools.call_tool_service("POST", "/admin/reset")


@app.get("/api/orders")
async def list_orders():
    return await tools.call_tool_service("GET", "/supplier-orders")


@app.post("/api/orders/{order_id}/send")
async def send_order(order_id: str):
    return await tools.call_tool_service("POST", f"/supplier-orders/{order_id}/send")


class Switches(BaseModel):
    ai_off: bool
    supplier_down: bool


@app.get("/api/switches")
def get_switches():
    return switches


@app.post("/api/switches")
async def set_switches(body: Switches):
    async with httpx.AsyncClient(timeout=5, headers=cloud.auth_headers(SUPPLIER_URL)) as client:
        await client.post(f"{SUPPLIER_URL}/switch", json={"down": body.supplier_down})
    switches.update(body.model_dump())
    return switches
