"""Fixed rules that produce the same offer as the agent, without AI.

Used when AI is switched off, or when Gemini fails or returns something
invalid. The scripted test model also uses build_offer.
"""

import tools

MAX_DISCOUNT = {"Gold": 15, "Silver": 10, "Bronze": 5}


def build_offer(loyalty: dict, stock: dict, substitutes: list[dict], order: dict) -> dict:
    tier, first = loyalty.get("tier", "Bronze"), loyalty.get("first_name", "there")
    name, sku = stock.get("name", stock["sku"]), stock["sku"]
    if stock.get("available", 0) > 0:
        return {"sold_out": False, "offer_type": "ship_from_other_warehouse", "sku": sku, "discount_percent": 0,
                "supplier_order_id": None,
                "message_to_customer": f"Hi {first}, good news: {name} is in stock at another warehouse"
                                       " and will ship from there."}
    discount = MAX_DISCOUNT.get(tier, 0)
    if tier == "Gold":
        offer = {"offer_type": "priority_reservation", "sku": sku,
                 "message_to_customer": f"Hi {first}, {name} sold out, so we have reserved the next one for you"
                                        f" with {discount}% off as a thank you for being Gold."}
    elif substitutes:
        sub = substitutes[0]
        offer = {"offer_type": "substitute", "sku": sub["sku"],
                 "message_to_customer": f"Hi {first}, {name} sold out, but {sub['name']} is in stock now"
                                        f" with {discount}% off."}
    else:
        discount = 0
        offer = {"offer_type": "back_in_stock_alert", "sku": sku,
                 "message_to_customer": f"Hi {first}, {name} sold out. We will tell you as soon as it is back."}
    return offer | {"sold_out": True, "discount_percent": discount, "supplier_order_id": order.get("order_id")}


async def decide(stockout: dict, log=lambda tool, args, result: None) -> dict:
    """Same steps as the agent, in a fixed order. log(tool, args, result) records each call."""
    async def step(name, func, **args):
        result = await func(**args)
        log(name, args, result)
        return result

    stock = await step("check_stock", tools.check_stock, sku=stockout["sku"])
    loyalty = await step("check_loyalty", tools.check_loyalty, customer_id=stockout["customer_id"])
    stock.setdefault("sku", stockout["sku"])
    if stock.get("available", 0) > 0:
        return build_offer(loyalty, stock, [], {})
    found = await step("find_similar_products", tools.find_similar_products,
                       query=stock.get("name", stockout["sku"]), exclude_sku=stockout["sku"])
    order = await step("order_from_supplier", tools.submit_order, sku=stockout["sku"],
                       quantity=stock.get("suggested_reorder_quantity", 10),
                       priority="urgent" if loyalty.get("tier") == "Gold" else "normal",
                       idempotency_key=f"{stockout['event_id']}:{stockout['sku']}")
    return build_offer(loyalty, stock, found.get("results", []), order)
