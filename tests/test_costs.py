"""Учёт расхода LLM: токены из ответа, стоимость по тарифу, запись в базу."""

import support  # общий стенд: изолированные данные, сеть запрещена

import json
import shutil
import unittest
from decimal import Decimal
from unittest.mock import patch

import httpx

from kpbot.config import BASE_DIR
from kpbot.llm import (
    LlmAnswer,
    LlmClient,
    LlmConfig,
    LlmLine,
    LlmPayload,
    LlmUsage,
    estimate_cost_rub,
)


def make_client(handler) -> LlmClient:
    return LlmClient(
        LlmConfig(base_url="https://llm.example/v1", model="m", api_key="sk-test-1234567890", retries=0),
        transport=httpx.MockTransport(handler),
    )


class UsageParsingTest(unittest.TestCase):
    def test_usage_is_read_from_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps({"lines": []})}}],
                    "usage": {"prompt_tokens": 812, "completion_tokens": 64, "total_tokens": 876},
                },
            )

        answer = make_client(handler).complete("текст")
        self.assertEqual(answer.usage.prompt_tokens, 812)
        self.assertEqual(answer.usage.completion_tokens, 64)
        self.assertEqual(answer.usage.total, 876)

    def test_missing_usage_gives_zeros(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"lines": []})}}]})

        answer = make_client(handler).complete("текст")
        self.assertEqual(answer.usage.total, 0)


class CostMathTest(unittest.TestCase):
    def test_zero_prices_means_unknown_cost(self) -> None:
        usage = LlmUsage(prompt_tokens=1000, completion_tokens=500)
        self.assertEqual(estimate_cost_rub(usage, 0, 0), Decimal("0"))

    def test_cost_by_tariff(self) -> None:
        usage = LlmUsage(prompt_tokens=1_000_000, completion_tokens=500_000)
        # 100 ₽ за миллион входа и 200 ₽ за миллион ответа
        cost = estimate_cost_rub(usage, 100.0, 200.0)
        self.assertEqual(cost, Decimal("200.0000"))

    def test_small_usage_is_not_zeroed(self) -> None:
        usage = LlmUsage(prompt_tokens=900, completion_tokens=100)
        cost = estimate_cost_rub(usage, 100.0, 200.0)
        self.assertGreater(cost, Decimal("0"))


class PipelineUsageTest(unittest.TestCase):
    def setUp(self) -> None:
        from kpbot.config import Settings
        from kpbot.pipeline import Pipeline
        from kpbot.store import Store

        self.data_dir = support.ROOT / "costs"
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.settings = Settings(
            data_dir=self.data_dir,
            api_key="test-key",
            ui_password="test-pass",
            session_secret="test-secret",
            pdf_enabled=False,
            llm_enabled=True,
            llm_base_url="https://llm.example/v1",
            llm_model="test-model",
            llm_api_key="sk-test-1234567890abcdef",
            llm_price_input_rub_per_1m=100.0,
            llm_price_output_rub_per_1m=200.0,
        )
        self.store = Store(self.settings.db_path)
        self.pipeline = Pipeline(self.store, self.settings)

    def tearDown(self) -> None:
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_usage_is_saved_with_cost(self) -> None:
        answer = LlmAnswer(
            payload=LlmPayload(lines=[LlmLine(name="уголок 50х50", qty=10, unit="шт")]),
            usage=LlmUsage(prompt_tokens=1000, completion_tokens=500),
        )
        with patch("kpbot.llm.LlmClient.complete", return_value=answer):
            result = self.pipeline.process_text(
                "уголок 50х50 — 10 шт", sender="a@b.ru", subject="Заявка"
            )
        usage = self.store.llm_usage_for(result.rfq_id)
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["prompt_tokens"], 1000)
        self.assertEqual(usage["completion_tokens"], 500)
        # 1000/1e6*100 + 500/1e6*200 = 0.1 + 0.1 = 0.2
        self.assertAlmostEqual(usage["cost_rub"], 0.2, places=4)

        totals = self.store.llm_totals()
        self.assertEqual(totals["rfqs"], 1)
        self.assertAlmostEqual(totals["cost_rub"], 0.2, places=4)


if __name__ == "__main__":
    unittest.main()
