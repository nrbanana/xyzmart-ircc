"""End-to-end runs with the scripted model: stock-out event -> agent -> checked offer."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import agent_main, push, tool_main
from fastapi.testclient import TestClient

import store
import tools

NOW = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731

@pytest.fixture(autouse=True)
def in_process_tool_service(supplier, monkeypatch):
    monkeypatch.setattr(tools, "TRANSPORT", httpx.ASGITransport(app=tool_main.app))


def run_stockout(customer_id, sku, event_id, warehouse="SYD"):
    event = {"event_id": event_id, "sku": sku, "warehouse": warehouse, "customer_id": customer_id,
             "occurred_at": NOW()}
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
    store.set_setting("ai_off", "switched on the demo screen")
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


def test_budget_kill_switch_moves_the_agent_to_fixed_rules():
    body = TestClient(tool_main.app).post("/pubsub/budget", json=push(
        {"costAmount": 50.4, "budgetAmount": 50, "currencyCode": "AUD"})).json()
    assert body == {"result": "ai switched off"}
    run = run_stockout("C-1002", "CM-100", "T-10")
    assert run["source"] == "rules"
    assert "budget reached: 50.40 of 50.00 AUD" in run["note"]
    assert run["check"]["offer"]["offer_type"] == "substitute"  # customers still get an offer


def test_token_ceiling_stops_the_model_and_uses_rules(monkeypatch):
    monkeypatch.setattr(agent_main, "TOKEN_CEILING", -1)  # the scripted model reports 0 tokens
    run = run_stockout("C-1002", "CM-100", "T-11")
    assert run["source"] == "rules"
    assert "token ceiling" in run["note"]


def test_front_door_contract_matches_the_code():
    """The Apigee OpenAPI file and the agent's StockoutEvent model must describe the same event."""
    import typing
    from pathlib import Path

    import yaml
    spec = yaml.safe_load((Path(__file__).parents[2] / "specialization/apigee/stockout-proxy/apiproxy"
                           / "resources/oas/stockout-api.yaml").read_text())["components"]["schemas"]["StockoutEvent"]
    model = agent_main.StockoutEvent.model_fields
    assert set(spec["required"]) == {name for name, field in model.items() if field.is_required()}
    assert spec["properties"]["warehouse"]["enum"] == list(typing.get_args(model["warehouse"].annotation))
    for field in ("event_id", "sku", "customer_id"):
        max_length = next(m.max_length for m in model[field].metadata if getattr(m, "max_length", None))
        assert spec["properties"][field]["maxLength"] == max_length


def test_demo_screen_is_never_served_stale_after_a_release():
    assert TestClient(agent_main.app).get("/").headers["cache-control"] == "no-cache"


def test_stale_event_gets_no_offer_and_no_order(published):
    old = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    body = asyncio.run(agent_main.on_stockout(push({"event_id": "T-12", "sku": "HP-100", "warehouse": "SYD",
                                                    "customer_id": "C-1001", "occurred_at": old})))
    assert body["status"] == "ignored" and "stale" in body["reason"]
    assert "T-12" not in agent_main.runs and published == []


def test_event_without_time_zone_is_refused():
    body = asyncio.run(agent_main.on_stockout(push({"event_id": "T-13", "sku": "HP-100", "warehouse": "SYD",
                                                    "customer_id": "C-1001", "occurred_at": "2026-10-24T09:15:02"})))
    assert body["status"] == "ignored"


def test_second_sell_out_for_same_customer_and_product_gets_no_second_discount():
    first = run_stockout("C-1002", "CM-100", "T-14")
    second = run_stockout("C-1002", "CM-100", "T-15")
    assert first["check"]["offer"]["discount_percent"] == 10
    assert second["check"]["offer"]["discount_percent"] == 0
    assert any("already had a discount" in p for p in second["check"]["problems"])
    assert second["check"]["offer"]["offer_type"] == "substitute"  # still helped, just not discounted again
