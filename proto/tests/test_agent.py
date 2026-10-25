"""End-to-end runs with the scripted model: stock-out event -> agent -> checked offer."""

import asyncio

import httpx
import pytest
from conftest import agent_main, push, tool_main

import tools

@pytest.fixture(autouse=True)
def in_process_tool_service(supplier, monkeypatch):
    monkeypatch.setattr(tools, "TRANSPORT", httpx.ASGITransport(app=tool_main.app))
    agent_main.switches["ai_off"] = False


def run_stockout(customer_id, sku, event_id, warehouse="SYD"):
    event = {"event_id": event_id, "sku": sku, "warehouse": warehouse, "customer_id": customer_id,
             "occurred_at": "2026-10-24T09:15:02Z"}
    asyncio.run(agent_main.on_stockout(push(event)))
    return agent_main.runs[event_id]


def tools_called(run):
    return [s["tool"] for s in run["steps"]]


def test_customer_purchase_flows_through_to_an_offer(published):
    # 1. The web shop (played by the demo screen) finds no stock and publishes a stock-out event.
    body = asyncio.run(agent_main.purchase(agent_main.Purchase(customer_id="C-1003", sku="CM-100", warehouse="SYD")))
    assert body["result"] == "out_of_stock"
    topic, event = published[0]
    assert topic == "stockout-events" and event["event_id"].startswith("WEB-")
    # 2. The event is delivered to the agent service.
    asyncio.run(agent_main.on_stockout(push(event)))
    run = agent_main.runs[event["event_id"]]
    # 3. The offer is checked and published for the customer.
    assert run["check"]["offer"]["offer_type"] == "substitute"
    assert published[-1][0] == "customer-offers"


def test_gold_gets_priority_reservation_and_order_waits_for_approval():
    run = run_stockout("C-1001", "HP-100", "T-1")
    assert run["status"] == "done" and run["source"] == "ai"
    assert tools_called(run) == ["check_stock", "check_loyalty", "find_similar_products", "order_from_supplier"]
    assert run["steps"][-1]["output"]["status"] == "PENDING_APPROVAL"  # 120 units > 50
    assert run["check"]["approved"] is True
    assert run["check"]["offer"]["offer_type"] == "priority_reservation"
    assert run["check"]["offer"]["discount_percent"] == 15
    assert run["check"]["offer"]["message_to_customer"].startswith("Hi Priya")


def test_silver_gets_in_stock_substitute():
    run = run_stockout("C-1002", "CM-100", "T-2")
    assert run["check"]["offer"] | {"message_to_customer": ""} == {
        "sold_out": True, "offer_type": "substitute", "sku": "CM-200", "discount_percent": 10,
        "message_to_customer": "", "supplier_order_id": run["steps"][-1]["output"]["order_id"]}


def test_false_alarm_ships_from_other_warehouse_without_reorder():
    run = run_stockout("C-1001", "HP-300", "T-8")  # SYD empty, MEL has 12
    assert tools_called(run) == ["check_stock", "check_loyalty"]
    assert run["source"] == "ai"
    assert run["check"]["approved"] is True
    assert run["check"]["offer"]["offer_type"] == "ship_from_other_warehouse"
    assert run["check"]["offer"]["sold_out"] is False


def test_injection_is_refused_even_though_model_obeyed_it():
    run = run_stockout("C-1004", "CM-100", "T-3")
    assert run["proposed_offer"]["discount_percent"] == 100  # the scripted model fell for it
    assert run["check"]["approved"] is False
    assert run["check"]["offer"]["discount_percent"] == 5  # the tool service did not


def test_purchase_with_stock_sends_no_event(published):
    body = asyncio.run(agent_main.purchase(agent_main.Purchase(customer_id="C-1003", sku="SN-100", warehouse="SYD")))
    assert body == {"result": "sold"}
    assert published == []


def test_duplicate_event_is_skipped():
    run_stockout("C-1003", "HP-100", "T-4")
    assert run_stockout("C-1003", "HP-100", "T-4")["status"] == "skipped"


def test_bad_event_is_acknowledged_and_never_reaches_the_model():
    body = asyncio.run(agent_main.on_stockout(push({"event_id": "T-9", "sku": "HP-100", "warehouse": "PER"})))
    assert body["status"] == "ignored"
    assert "T-9" not in agent_main.runs


def test_ai_off_uses_rules_with_same_format():
    agent_main.switches["ai_off"] = True
    run = run_stockout("C-1003", "HP-100", "T-5")
    assert run["source"] == "rules"
    assert run["check"]["approved"] is True
    assert run["check"]["offer"]["discount_percent"] == 5


def test_answer_without_tool_calls_falls_back_to_rules(monkeypatch):
    async def lazy_agent(stockout, run):  # model answers straight away, no tools, no reorder
        return agent_main.agent.Offer(sold_out=True, offer_type="back_in_stock_alert", sku=stockout["sku"],
                                      discount_percent=0, message_to_customer="Sorry")
    monkeypatch.setattr(agent_main, "run_agent", lazy_agent)
    run = run_stockout("C-1002", "CM-100", "T-7")
    assert run["source"] == "rules"
    assert "skipped" in run["note"]
    assert run["check"]["offer"]["offer_type"] == "substitute"


def test_supplier_down_still_sends_substitute(supplier):
    supplier["down"] = True
    run = run_stockout("C-1002", "CM-100", "T-6")
    assert run["steps"][-1]["output"]["status"] == "QUEUED"
    assert run["check"]["offer"]["offer_type"] == "substitute"
