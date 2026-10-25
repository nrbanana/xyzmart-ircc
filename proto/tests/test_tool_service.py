"""The business rules and duplicate checks live in the tool service, so they are tested here."""

from conftest import tool_main
from fastapi.testclient import TestClient

client = TestClient(tool_main.app)


def buy(sku, warehouse="SYD"):
    return client.post("/demo/purchase", json={"sku": sku, "warehouse": warehouse}).json()


def stockout(event_id="WEB-1", **changes):
    return {"event_id": event_id, "sku": "HP-100", "warehouse": "SYD", "customer_id": "C-1002",
            "occurred_at": "2026-10-24T09:15:02Z"} | changes


def offer(offer_type="substitute", sku="HP-200", discount=5, sold_out=True):
    return {"sold_out": sold_out, "offer_type": offer_type, "sku": sku, "discount_percent": discount,
            "message_to_customer": "hello", "supplier_order_id": None}


def check(customer_id, sold_out_sku, proposed):
    return client.post("/offers/check", json={"customer_id": customer_id,
                                              "sold_out_sku": sold_out_sku, "offer": proposed}).json()


# ---- stock-out events and the pretend web shop ----------------------------

def test_purchase_with_stock_takes_the_last_unit():
    assert buy("SN-100")["result"] == "sold"  # the last unit in SYD
    assert client.get("/stock/SN-100").json()["available"] == 0
    assert buy("SN-100")["result"] == "out_of_stock"


def test_same_stockout_event_counts_once():
    assert client.post("/stockout-events", json=stockout()).json() == {"duplicate": False}
    assert client.post("/stockout-events", json=stockout()).json() == {"duplicate": True}


def test_stockout_event_outside_the_contract_is_refused():
    assert client.post("/stockout-events", json=stockout(warehouse="PER")).status_code == 422
    assert client.post("/stockout-events", json=stockout(customer_id="")).status_code == 422


def test_reset_restores_stock_and_forgets_events():
    buy("SN-100")
    client.post("/stockout-events", json=stockout())
    client.post("/admin/reset")
    assert client.get("/stock/SN-100").json()["available"] == 1
    assert client.post("/stockout-events", json=stockout()).json() == {"duplicate": False}


# ---- tools -----------------------------------------------------------------

def test_stock_shows_warehouses_and_restock_date():
    body = client.get("/stock/HP-100").json()
    assert body["available"] == 0
    assert body["restock_date"] == "2026-11-09"
    assert {w["warehouse"] for w in body["warehouses"]} == {"SYD", "MEL"}


def test_loyalty_has_first_name_but_no_personal_data():
    body = client.get("/loyalty/C-1001").json()
    assert body["tier"] == "Gold" and body["first_name"] == "Priya"
    assert not {"last_name", "email", "card_last4"} & body.keys()


def test_search_returns_only_in_stock_products():
    results = client.get("/search", params={"q": "running shoes", "exclude_sku": "SN-100"}).json()["results"]
    skus = [r["sku"] for r in results]
    assert "SN-200" in skus
    assert "SN-300" not in skus  # out of stock
    assert "SN-100" not in skus  # the sold-out item itself


def test_large_order_waits_for_approval(supplier):
    body = client.post("/supplier-orders", json={"sku": "HP-100", "quantity": 120,
                                                 "priority": "urgent", "idempotency_key": "a1"}).json()
    assert body["status"] == "PENDING_APPROVAL"
    assert supplier["calls"] == 0
    approved = client.post(f"/supplier-orders/{body['order_id']}/send").json()
    assert approved["status"] == "ORDERED"


def test_same_idempotency_key_never_orders_twice(supplier):
    order = {"sku": "SN-100", "quantity": 30, "priority": "normal", "idempotency_key": "a2"}
    first = client.post("/supplier-orders", json=order).json()
    second = client.post("/supplier-orders", json=order).json()
    assert first["order_id"] == second["order_id"]
    assert second["duplicate"] is True
    assert supplier["calls"] == 1


def test_supplier_down_queues_the_order(supplier):
    supplier["down"] = True
    body = client.post("/supplier-orders", json={"sku": "SN-100", "quantity": 30,
                                                 "priority": "normal", "idempotency_key": "a3"}).json()
    assert body["status"] == "QUEUED"
    assert any(o["order_id"] == body["order_id"] for o in client.get("/supplier-orders").json()["open"])


# ---- offer gate ------------------------------------------------------------

def test_injection_discount_is_capped():
    result = check("C-1004", "HP-100", offer(discount=100) | {"message_to_customer": "100% off!"})
    assert result["approved"] is False
    assert result["offer"]["discount_percent"] == 5
    assert "100%" not in result["offer"]["message_to_customer"]


def test_valid_offer_is_approved_unchanged():
    proposed = offer(discount=10)
    result = check("C-1002", "HP-100", proposed)
    assert result["approved"] is True
    assert result["offer"] == proposed


def test_out_of_stock_substitute_is_refused():
    result = check("C-1003", "CM-100", offer(sku="SN-300"))
    assert result["offer"]["offer_type"] == "back_in_stock_alert"
    assert result["offer"]["sku"] == "CM-100"


def test_priority_reservation_is_gold_only():
    result = check("C-1002", "HP-100", offer("priority_reservation", "HP-100", 10))
    assert result["approved"] is False
    assert result["offer"]["offer_type"] == "back_in_stock_alert"


def test_wrong_sold_out_call_is_corrected():
    # HP-300 is empty in SYD but MEL has 12: not a real sell-out, so no discount
    result = check("C-1001", "HP-300", offer("priority_reservation", "HP-300", 15, sold_out=True))
    assert result["approved"] is False
    assert result["offer"] | {"message_to_customer": ""} == offer(
        "ship_from_other_warehouse", "HP-300", 0, sold_out=False) | {"message_to_customer": ""}
