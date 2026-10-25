"""Scripted stand-in for Gemini, used by tests (LLM_MODE=mock). No network.

It calls the tools in the same order Gemini usually does. On purpose, it obeys
the prompt injection in the order note, so tests prove the tool service
refuses the bad offer even when the model goes along with it.
"""

import json
from typing import AsyncGenerator

from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types

import rules


def _call(name: str, **args) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


class MockLlm(BaseLlm):
    model: str = "mock"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        stockout, results = {}, {}
        for content in llm_request.contents:
            for part in content.parts or []:
                if part.text and part.text.strip().startswith("{") and not stockout:
                    stockout = json.loads(part.text)
                if part.function_response:
                    results[part.function_response.name] = part.function_response.response

        stock = results.get("check_stock", {})
        sold_out = stock.get("available", 0) == 0
        if not stock:
            parts = [_call("check_stock", sku=stockout["sku"]),
                     _call("check_loyalty", customer_id=stockout["customer_id"])]
        elif sold_out and "find_similar_products" not in results:
            parts = [_call("find_similar_products", query=stock.get("name", ""), exclude_sku=stockout["sku"])]
        elif sold_out and "order_from_supplier" not in results:
            parts = [_call("order_from_supplier", sku=stockout["sku"],
                           quantity=stock.get("suggested_reorder_quantity", 10),
                           priority="urgent" if results["check_loyalty"].get("tier") == "Gold" else "normal")]
        else:
            loyalty = results["check_loyalty"]
            offer = rules.build_offer(loyalty, stock, results.get("find_similar_products", {}).get("results", []),
                                      results.get("order_from_supplier", {}))
            if "100%" in loyalty.get("order_note", ""):
                offer["discount_percent"] = 100
                offer["message_to_customer"] = "As requested, it is free: 100% off!"
            if "set_model_response" in llm_request.tools_dict:
                parts = [_call("set_model_response", **offer)]
            else:
                parts = [types.Part(text=json.dumps(offer))]
        yield LlmResponse(content=types.Content(role="model", parts=parts))
