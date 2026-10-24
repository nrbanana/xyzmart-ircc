"""End-to-end runs with the scripted model: stock event -> alert -> agent -> checked offer."""

import asyncio

import httpx
import pytest
from conftest import agent_main, push, tool_main
from fastapi.testclient import TestClient

import tools

tool_client = TestClient(tool_main.app)


@pytest.fixture(autouse=True)
def in_process_tool_service(supplier, monkeypatch):
    monkeypatch.setattr(tools, "TRANSPORT", httpx.ASGITransport(app=tool_main.app))
    agent_main.handled_alert_ids.clear()
    agent_main.switches["ai_off"] = False


def run_alert(customer_id, sku, alert_id, warehouse="SYD"):
    alert = {"alert_id": alert_id, "sku": sku, "warehouse": warehouse, "customer_id": customer_id}
    asyncio.run(agent_main.on_alert(push(alert)))
    return agent_main.runs[alert_id]


def tools_called(run):
    return [s["tool"] for s in run["steps"]]


def test_customer_purchase_flows_through_to_an_offer(published):
    # 1. A sale arrives on the stock feed for an item with no stock left.
    tool_client.post("/pubsub/stock-events", json=push({
        "event_id": "EV-9", "type": "sale", "sku": "CM-100", "warehouse": "SYD", "customer_id": "C-1003"}))
    topic, alert = published[0]
    assert topic == "sold-out-alerts"
    # 2. The alert is delivered to the agent service.
    asyncio.run(agent_main.on_alert(push(alert)))
    run = agent_main.runs[alert["alert_id"]]
    # 3. The offer is checked and published for the customer.
    assert run["check"]["offer"]["offer_type"] == "substitute"
    assert published[-1][0] == "customer-offers"


def test_gold_gets_priority_reservation_and_order_waits_for_approval():
    run = run_alert("C-1001", "HP-100", "T-1")
    assert run["status"] == "done" and run["source"] == "ai"
    assert tools_called(run) == ["check_stock", "check_loyalty", "find_similar_products", "order_from_supplier"]
    assert run["steps"][-1]["output"]["status"] == "PENDING_APPROVAL"  # 120 units > 50
    assert run["check"]["approved"] is True
    assert run["check"]["offer"]["offer_type"] == "priority_reservation"
    assert run["check"]["offer"]["discount_percent"] == 15
    assert run["check"]["offer"]["message_to_customer"].startswith("Hi Priya")


def test_silver_gets_in_stock_substitute():
    run = run_alert("C-1002", "CM-100", "T-2")
    assert run["check"]["offer"] | {"message_to_customer": ""} == {
        "sold_out": True, "offer_type": "substitute", "sku": "CM-200", "discount_percent": 10,
        "message_to_customer": "", "supplier_order_id": run["steps"][-1]["output"]["order_id"]}


def test_false_alarm_ships_from_other_warehouse_without_reorder():
    run = run_alert("C-1001", "HP-300", "T-8")  # SYD empty, MEL has 12
    assert tools_called(run) == ["check_stock", "check_loyalty"]
    assert run["source"] == "ai"
    assert run["check"]["approved"] is True
    assert run["check"]["offer"]["offer_type"] == "ship_from_other_warehouse"
    assert run["check"]["offer"]["sold_out"] is False


def test_injection_is_refused_even_though_model_obeyed_it():
    run = run_alert("C-1004", "CM-100", "T-3")
    assert run["proposed_offer"]["discount_percent"] == 100  # the scripted model fell for it
    assert run["check"]["approved"] is False
    assert run["check"]["offer"]["discount_percent"] == 5  # the tool service did not


def test_duplicate_alert_is_skipped():
    run_alert("C-1003", "HP-100", "T-4")
    assert run_alert("C-1003", "HP-100", "T-4")["status"] == "skipped"


def test_ai_off_uses_rules_with_same_format():
    agent_main.switches["ai_off"] = True
    run = run_alert("C-1003", "HP-100", "T-5")
    assert run["source"] == "rules"
    assert run["check"]["approved"] is True
    assert run["check"]["offer"]["discount_percent"] == 5


def test_answer_without_tool_calls_falls_back_to_rules(monkeypatch):
    async def lazy_agent(alert, run):  # model answers straight away, no tools, no reorder
        return agent_main.agent.Offer(sold_out=True, offer_type="back_in_stock_alert", sku=alert["sku"],
                                      discount_percent=0, message_to_customer="Sorry")
    monkeypatch.setattr(agent_main, "run_agent", lazy_agent)
    run = run_alert("C-1002", "CM-100", "T-7")
    assert run["source"] == "rules"
    assert "skipped" in run["note"]
    assert run["check"]["offer"]["offer_type"] == "substitute"


def test_supplier_down_still_sends_substitute(supplier):
    supplier["down"] = True
    run = run_alert("C-1002", "CM-100", "T-6")
    assert run["steps"][-1]["output"]["status"] == "QUEUED"
    assert run["check"]["offer"]["offer_type"] == "substitute"
