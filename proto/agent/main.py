"""Agent service: receives out-of-stock alerts, runs the agent, sends offers, serves the demo screen.

Message flow (Pub/Sub in Google Cloud, direct HTTP on the laptop):
  demo "Customer buys"  -> stock-events     -> tool service (applies sale, detects sell-out)
  tool service          -> sold-out-alerts  -> this service, /pubsub/alerts (runs the agent)
  this service          -> customer-offers  -> customer channels (here: the demo screen)
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE.parent / ".env")  # before importing the agent, which reads the model name

import httpx  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import agent  # noqa: E402
import rules  # noqa: E402
import tools  # noqa: E402
from shared import cloud  # noqa: E402

SUPPLIER_URL = os.getenv("SUPPLIER_URL", "http://localhost:8082")
AGENT_TIMEOUT_SECONDS = 60

sessions = InMemorySessionService()
runner = Runner(agent=agent.root_agent, app_name="ircc", session_service=sessions)
offers: list[dict] = []
runs: dict[str, dict] = {}  # alert_id -> latest delivery of that alert, shown on the demo screen
handled_alert_ids: set[str] = set()
switches = {"ai_off": False, "supplier_down": False}


def required_tools(offer: dict) -> set[str]:
    """An offer is only trusted if it is grounded in these tool results."""
    return {"check_stock", "check_loyalty"} | ({"order_from_supplier"} if offer["sold_out"] else set())


async def run_agent(alert: dict, run: dict) -> agent.Offer:
    """One agent run. Each alert gets its own session: its working memory."""
    session = await sessions.create_session(app_name="ircc", user_id="ircc",
                                            state={"alert_id": alert["alert_id"]})
    message = types.Content(role="user", parts=[types.Part(text=json.dumps(alert))])
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


async def handle_alert(alert: dict, run: dict) -> None:
    if alert["alert_id"] in handled_alert_ids:
        run.update(status="skipped", note="Duplicate alert id, already handled. No second order.")
        return
    handled_alert_ids.add(alert["alert_id"])
    run["status"] = "running"
    try:
        if switches["ai_off"]:
            raise RuntimeError("AI switched off")
        offer = (await asyncio.wait_for(run_agent(alert, run), AGENT_TIMEOUT_SECONDS)).model_dump()
        skipped = required_tools(offer) - {step["tool"] for step in run["steps"]}
        if skipped:
            raise ValueError(f"agent skipped {', '.join(sorted(skipped))}")
        run["source"] = "ai"
    except Exception as e:  # Gemini down, slow, or invalid output: fall back to fixed rules
        run.update(source="rules", note=f"Rules fallback used: {e}")
        offer = await rules.decide(alert, log=lambda tool, args, result: run["steps"].append(
            {"tool": tool, "input": args, "output": result}))

    run["proposed_offer"] = offer
    check = await tools.call_tool_service("POST", "/offers/check", json={
        "customer_id": alert["customer_id"], "sold_out_sku": alert["sku"], "offer": offer})
    if "error" in check:
        run.update(status="error", note=f"Offer check failed: {check['error']}")
        return
    run.update(check=check, status="done")
    sent = {"alert_id": alert["alert_id"], "customer_id": alert["customer_id"], **check["offer"]}
    offers.append(sent)
    cloud.publish("customer-offers", sent)


app = FastAPI(title="xyzMart recovery agent")


@app.get("/")
def demo_screen():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/pubsub/alerts")
async def on_alert(envelope: dict):
    """Pub/Sub push endpoint for out-of-stock alerts. Answers 2xx once handled, so Pub/Sub stops retrying."""
    try:
        alert = cloud.read_push(envelope)
        alert = {k: alert[k] for k in ("alert_id", "sku", "warehouse", "customer_id")}
    except (KeyError, ValueError, TypeError) as e:
        return {"ignored": str(e)[:200]}
    run = {"alert": alert, "status": "received", "steps": [], "tokens": 0, "source": None, "note": None}
    runs[alert["alert_id"]] = run
    try:
        await handle_alert(alert, run)
    except Exception as e:
        run.update(status="error", note=str(e)[:300])
    return {"status": run["status"]}


# ---- demo screen API -------------------------------------------------------

class Purchase(BaseModel):
    customer_id: str
    sku: str
    warehouse: str


@app.post("/api/purchase")
def purchase(body: Purchase):
    """A customer buys one unit online. This becomes one event on the live stock feed."""
    event = {"event_id": "EV-" + uuid.uuid4().hex[:8].upper(), "type": "sale", "quantity": 1, **body.model_dump()}
    cloud.publish("stock-events", event)
    return event


@app.get("/api/events/{event_id}")
async def get_event(event_id: str):
    return await tools.call_tool_service("GET", f"/stock-events/{event_id}")


@app.get("/api/runs/{alert_id}")
def get_run(alert_id: str):
    if alert_id not in runs:
        raise HTTPException(404, "Alert not received yet")
    return runs[alert_id]


@app.post("/api/alerts/{alert_id}/redeliver")
def redeliver(alert_id: str):
    """Publish the same alert again, as Pub/Sub sometimes does."""
    if alert_id not in runs:
        raise HTTPException(404)
    cloud.publish("sold-out-alerts", runs[alert_id]["alert"])
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
    handled_alert_ids.clear()
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
