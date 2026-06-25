"""
Automatic LLM-based evaluation of query answers.
Runs asynchronously after every query. Scores on 3 dimensions (1-5 scale).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from src.llm.client import TrackedAnthropic

from src.config import Settings, get_settings
from src.models import EvaluationScore, QueryResponse

logger = logging.getLogger(__name__)


def should_sample(rate: float, draw: float) -> bool:
    """True if this query should be judged. `draw` is a [0,1) random value.

    rate <= 0 never samples; rate >= 1 always samples.
    """
    return draw < rate


_EVAL_PROMPT = """You are evaluating the quality of an answer produced by a city government knowledge base system.

Question: {question}

Retrieved context (what the system had access to):
{context}

Answer produced:
{answer}

Score this answer on three dimensions, each from 1 to 5:

1. **retrieval_score** (1-5): Did the system retrieve chunks that actually contain the answer?
   - 5: Perfect — retrieved chunks directly contain the answer
   - 3: Partial — some relevant chunks, but key information missing
   - 1: Failed — retrieved chunks are irrelevant to the question

2. **accuracy_score** (1-5): Does the answer correctly reflect what was in the retrieved chunks?
   - 5: Perfectly accurate
   - 3: Mostly correct with minor errors
   - 1: Contains errors or information not present in the chunks

3. **completeness_score** (1-5): Is the answer complete, or is important information missing?
   - 5: Fully complete
   - 3: Partially complete
   - 1: Major information missing

Also flag:
- **retrieval_failure**: true if no relevant chunks were retrieved at all
- **hallucination_detected**: true if the answer contains facts not present in the retrieved context

Return JSON:
{{
  "retrieval_score": 1-5,
  "accuracy_score": 1-5,
  "completeness_score": 1-5,
  "retrieval_failure": true|false,
  "hallucination_detected": true|false,
  "reasoning": "brief explanation"
}}
"""


class Evaluator:
    def __init__(self, settings: Optional[Settings] = None, llm: Optional[TrackedAnthropic] = None):
        self.cfg = settings or get_settings()
        self.client = llm or TrackedAnthropic(self.cfg, call_site="evaluation.evaluator")

    def evaluate(
        self,
        response: QueryResponse,
        retrieved_context: str,
    ) -> EvaluationScore:
        """
        Evaluate a query response. Called async after query completes.
        1 LLM call per evaluation.
        """
        prompt = _EVAL_PROMPT.format(
            question=response.question,
            context=retrieved_context[:4000],
            answer=response.answer,
        )

        try:
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model,
                max_tokens=512,
                query_id=response.query_id,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text
            return _parse_score(raw)
        except Exception as e:
            logger.error("Evaluation LLM call failed: %s", e)
            return EvaluationScore(
                retrieval_score=3.0,
                accuracy_score=3.0,
                completeness_score=3.0,
                reasoning=f"Evaluation failed: {e}",
            )


def _parse_score(raw: str) -> EvaluationScore:
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    json_str = match.group(1) if match else raw

    try:
        data = json.loads(json_str)
        return EvaluationScore(
            retrieval_score=float(data.get("retrieval_score", 3)),
            accuracy_score=float(data.get("accuracy_score", 3)),
            completeness_score=float(data.get("completeness_score", 3)),
            retrieval_failure=bool(data.get("retrieval_failure", False)),
            hallucination_detected=bool(data.get("hallucination_detected", False)),
            reasoning=data.get("reasoning", ""),
        )
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Could not parse evaluation score: %s", e)
        return EvaluationScore(
            retrieval_score=3.0,
            accuracy_score=3.0,
            completeness_score=3.0,
            reasoning="Parse error",
        )
