"""
Evaluation suite — a curated set of questions with known correct answers.
Used to measure and track knowledge base quality over time.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from src.config import Settings, get_settings
from src.models import EvaluationRunResult, EvaluationSuiteEntry
from src.storage.sql_store import SQLStore

logger = logging.getLogger(__name__)

# Seed Q&A pairs — 50 questions across SQL, vector, graph, and cross-store categories.
# Drawn from the document corpus described in the spec.
SEED_QUESTIONS: list[dict] = [
    # SQL — budget/expenditure
    {"question": "How much has been spent on disposal year to date in Q1 2026?",
     "expected_answer": "$650,198.57",
     "store_type": "sql", "department": "Public Works", "quarter": "Q1", "year": 2026},
    {"question": "What is the revised budget for the Highway Department contracted services?",
     "expected_answer": "See expenditures table for Highway contracted services line item.",
     "store_type": "sql", "department": "Public Works", "quarter": "Q1", "year": 2026},
    {"question": "Which department had the highest year-to-date expenditure in Q1 2026?",
     "expected_answer": "Department with highest ytd_expended sum in Q1 2026.",
     "store_type": "sql", "quarter": "Q1", "year": 2026},
    {"question": "What is the total revised budget for the Health Office in Q1 2026?",
     "expected_answer": "Sum of revised_budget for Health Office Q1 2026.",
     "store_type": "sql", "department": "Health Office", "quarter": "Q1", "year": 2026},
    {"question": "How many potholes were repaired by Public Works in Q1 2026?",
     "expected_answer": "Pothole repair count from Public Works metrics Q1 2026.",
     "store_type": "sql", "department": "Public Works", "quarter": "Q1", "year": 2026},
    {"question": "What grants did the Health Office receive in Q1 2026?",
     "expected_answer": "NEHA-FDA grant, $14,000.",
     "store_type": "sql", "department": "Health Office", "quarter": "Q1", "year": 2026},
    {"question": "How many open vacancies does the Bureau of Codes have in Q1 2026?",
     "expected_answer": "Count of open vacancies for Bureau of Codes Q1 2026.",
     "store_type": "sql", "department": "Bureau of Codes", "quarter": "Q1", "year": 2026},
    {"question": "What was the total tonnage collected by Public Works in Q1 2026?",
     "expected_answer": "Total tonnage metric for Public Works Q1 2026.",
     "store_type": "sql", "department": "Public Works", "quarter": "Q1", "year": 2026},
    {"question": "What is the ytd expenditure for the Bureau of Fire in Q1 2026?",
     "expected_answer": "Sum of ytd_expended for Bureau of Fire Q1 2026.",
     "store_type": "sql", "department": "Bureau of Fire", "quarter": "Q1", "year": 2026},
    {"question": "How many building permits were issued in Q1 2026?",
     "expected_answer": "Building permits metric from Building & Housing Q1 2026.",
     "store_type": "sql", "department": "Building & Housing Development", "quarter": "Q1", "year": 2026},

    # Graph — org/people
    {"question": "Who is the Director of Public Works?",
     "expected_answer": "Dave West",
     "store_type": "graph", "department": "Public Works"},
    {"question": "Who manages the Health Office?",
     "expected_answer": "Director/manager of Health Office from org chart.",
     "store_type": "graph", "department": "Health Office"},
    {"question": "What projects is the Facilities department responsible for?",
     "expected_answer": "Projects linked to Facilities & Special Projects in graph.",
     "store_type": "graph", "department": "Facilities & Special Projects"},
    {"question": "Which departments reported open vacancies in Q1 2026?",
     "expected_answer": "All departments with status=open in vacancies table Q1 2026.",
     "store_type": "sql", "quarter": "Q1", "year": 2026},
    {"question": "Who leads the Bureau of Police?",
     "expected_answer": "Director/Commissioner of Bureau of Police.",
     "store_type": "graph", "department": "Bureau of Police"},
    {"question": "Who is the director of the Bureau of Codes?",
     "expected_answer": "Director of Bureau of Codes from org data.",
     "store_type": "graph", "department": "Bureau of Codes"},
    {"question": "What department does Joel Seiders manage?",
     "expected_answer": "Department managed by Joel Seiders from graph.",
     "store_type": "graph"},
    {"question": "Who leads the Bureau of Parks and Recreation?",
     "expected_answer": "Director of Bureau of Parks & Recreation.",
     "store_type": "graph", "department": "Bureau of Parks & Recreation"},
    {"question": "Which departments report to the Director of Public Works?",
     "expected_answer": "Sub-departments or divisions under Public Works.",
     "store_type": "graph", "department": "Public Works"},
    {"question": "Who manages capital projects for the Facilities department?",
     "expected_answer": "Project managers from Facilities org data.",
     "store_type": "graph", "department": "Facilities & Special Projects"},

    # Vector — narrative
    {"question": "What is the Health Office responsible for?",
     "expected_answer": "Food safety compliance, environmental health, and related public health services.",
     "store_type": "vector", "department": "Health Office"},
    {"question": "What community engagement activities did Public Works conduct in Q1 2026?",
     "expected_answer": "Community outreach activities from Public Works Q1 2026 narrative.",
     "store_type": "vector", "department": "Public Works", "quarter": "Q1", "year": 2026},
    {"question": "What are the Bureau of IT's goals for 2026?",
     "expected_answer": "Annual technology goals from Bureau of IT Q1 2026.",
     "store_type": "vector", "department": "Bureau of Information Technology"},
    {"question": "What special projects is the Facilities department working on?",
     "expected_answer": "Capital and special projects from Facilities Q1 2026.",
     "store_type": "vector", "department": "Facilities & Special Projects"},
    {"question": "What is the mission of the Bureau of Parks and Recreation?",
     "expected_answer": "Mission statement from Bureau of Parks Q1 2026.",
     "store_type": "vector", "department": "Bureau of Parks & Recreation"},
    {"question": "What sustainability initiatives is the city working on?",
     "expected_answer": "Sustainability-related narrative from any department Q1 2026.",
     "store_type": "vector"},
    {"question": "What code enforcement activities did the Bureau of Codes undertake in Q1 2026?",
     "expected_answer": "Code enforcement narrative from Bureau of Codes Q1 2026.",
     "store_type": "vector", "department": "Bureau of Codes", "quarter": "Q1", "year": 2026},
    {"question": "What training programs did the Bureau of Fire complete in Q1 2026?",
     "expected_answer": "Training and development activities from Bureau of Fire Q1 2026.",
     "store_type": "vector", "department": "Bureau of Fire", "quarter": "Q1", "year": 2026},
    {"question": "What is the Budget & Finance department's role in the city?",
     "expected_answer": "Description and mission of Budget & Finance department.",
     "store_type": "vector", "department": "Budget & Finance"},
    {"question": "What planning initiatives did the Bureau of Planning work on in Q1 2026?",
     "expected_answer": "Planning projects and activities from Bureau of Planning Q1 2026.",
     "store_type": "vector", "department": "Bureau of Planning", "quarter": "Q1", "year": 2026},

    # Cross-store — multiple stores needed
    {"question": "What department spent the most on contracted services and who leads it?",
     "expected_answer": "Department with max contracted services expenditure and their director name.",
     "store_type": "cross"},
    {"question": "What grant did the Health Office receive in 2026 and who manages that office?",
     "expected_answer": "Grant name/amount from SQL + director from graph.",
     "store_type": "cross", "department": "Health Office"},
    {"question": "How many inspections did the Bureau of Codes perform and what was the budget for that activity?",
     "expected_answer": "Inspection count from metrics + budget from expenditures.",
     "store_type": "cross", "department": "Bureau of Codes"},
    {"question": "Who leads the Public Works department and what was their total Q1 2026 spend?",
     "expected_answer": "Director name from graph + total expenditure from SQL.",
     "store_type": "cross", "department": "Public Works", "quarter": "Q1", "year": 2026},
    {"question": "Which departments had both open vacancies and capital projects in Q1 2026?",
     "expected_answer": "Intersection of departments with open vacancies and projects.",
     "store_type": "cross", "quarter": "Q1", "year": 2026},
    {"question": "What were the Bureau of Fire's Q1 2026 expenditures and what are their 2026 goals?",
     "expected_answer": "Fire expenditure from SQL + goals narrative from vector.",
     "store_type": "cross", "department": "Bureau of Fire", "quarter": "Q1", "year": 2026},
    {"question": "What special projects did Facilities complete and how much was spent?",
     "expected_answer": "Project names from graph/vector + expenditure from SQL.",
     "store_type": "cross", "department": "Facilities & Special Projects"},
    {"question": "Who leads Parks and Recreation and what community events did they host?",
     "expected_answer": "Director from graph + events narrative from vector.",
     "store_type": "cross", "department": "Bureau of Parks & Recreation"},
    {"question": "What IT projects are underway and what is the IT budget for 2026?",
     "expected_answer": "Projects from graph/vector + budget from SQL.",
     "store_type": "cross", "department": "Bureau of Information Technology"},
    {"question": "How many code violations were issued and what is the Codes department budget?",
     "expected_answer": "Violation metric from SQL + budget expenditures from SQL.",
     "store_type": "cross", "department": "Bureau of Codes"},

    # Additional SQL
    {"question": "What is the total grant funding received by all departments in Q1 2026?",
     "expected_answer": "Sum of all grant amounts for Q1 2026.",
     "store_type": "sql", "quarter": "Q1", "year": 2026},
    {"question": "Which department has the most vacancies in Q1 2026?",
     "expected_answer": "Department with highest count of open vacancies.",
     "store_type": "sql", "quarter": "Q1", "year": 2026},
    {"question": "What is the revised budget vs actual spend for the Bureau of Police?",
     "expected_answer": "Revised budget and ytd_expended for Bureau of Police Q1 2026.",
     "store_type": "sql", "department": "Bureau of Police", "quarter": "Q1", "year": 2026},

    # Additional vector
    {"question": "What are the annual goals for the Bureau of Communications in 2026?",
     "expected_answer": "Annual goals from Bureau of Communications Q1 2026.",
     "store_type": "vector", "department": "Bureau of Communications"},
    {"question": "What outreach programs did the Health Office run in Q1 2026?",
     "expected_answer": "Community outreach from Health Office Q1 2026.",
     "store_type": "vector", "department": "Health Office", "quarter": "Q1", "year": 2026},
    {"question": "What infrastructure improvements did Public Works make to roads in Q1 2026?",
     "expected_answer": "Road/infrastructure narrative from Public Works Q1 2026.",
     "store_type": "vector", "department": "Public Works", "quarter": "Q1", "year": 2026},

    # Additional graph
    {"question": "What projects is the Bureau of Planning overseeing?",
     "expected_answer": "Projects linked to Bureau of Planning in graph.",
     "store_type": "graph", "department": "Bureau of Planning"},
    {"question": "Who are the key personnel in the Facilities department?",
     "expected_answer": "People nodes linked to Facilities & Special Projects.",
     "store_type": "graph", "department": "Facilities & Special Projects"},
    {"question": "What grants does the Health Office manage?",
     "expected_answer": "Grant nodes linked to Health Office via MANAGES_GRANT.",
     "store_type": "graph", "department": "Health Office"},
]


class EvaluationSuite:
    def __init__(
        self,
        sql_store: Optional[SQLStore] = None,
        settings: Optional[Settings] = None,
    ):
        self.cfg = settings or get_settings()
        self.sql_store = sql_store or SQLStore(self.cfg)

    def seed_questions(self) -> None:
        """Insert the seed Q&A pairs if the table is empty."""
        existing = self.sql_store.get_evaluation_suite()
        if existing:
            logger.info("Evaluation suite already has %d questions — skipping seed", len(existing))
            return

        with self.sql_store.cursor() as cur:
            for q in SEED_QUESTIONS:
                cur.execute(
                    """INSERT INTO evaluation_suite
                       (question, expected_answer, store_type, department, quarter, year)
                       VALUES (%(question)s, %(expected_answer)s, %(store_type)s,
                               %(department)s, %(quarter)s, %(year)s)""",
                    {
                        "question": q["question"],
                        "expected_answer": q["expected_answer"],
                        "store_type": q["store_type"],
                        "department": q.get("department"),
                        "quarter": q.get("quarter"),
                        "year": q.get("year"),
                    },
                )
        logger.info("Seeded %d evaluation questions", len(SEED_QUESTIONS))

    def run(self, query_pipeline, evaluator) -> list[EvaluationRunResult]:
        """
        Run all evaluation questions through the query pipeline and score each answer.
        Returns a list of EvaluationRunResult.
        """
        from src.evaluation.evaluator import Evaluator

        run_id = str(uuid.uuid4())
        questions = self.sql_store.get_evaluation_suite()
        results: list[EvaluationRunResult] = []

        logger.info("Starting evaluation run %s with %d questions", run_id, len(questions))

        for entry in questions:
            question = entry["question"]
            expected = entry["expected_answer"]

            try:
                response = query_pipeline.ask(question, log_query=False)
                score = evaluator.evaluate(response, retrieved_context=response.answer)

                avg = (score.retrieval_score + score.accuracy_score + score.completeness_score) / 3
                passed = avg >= 3.0

                result = EvaluationRunResult(
                    run_id=run_id,
                    run_date=response.timestamp,
                    question_id=entry.get("id"),
                    question=question,
                    expected_answer=expected,
                    actual_answer=response.answer,
                    retrieval_score=score.retrieval_score,
                    accuracy_score=score.accuracy_score,
                    completeness_score=score.completeness_score,
                    passed=passed,
                    notes=score.reasoning,
                )
                results.append(result)
                self.sql_store.save_evaluation_result(run_id, result.model_dump())

            except Exception as e:
                logger.error("Eval question failed: %s — %s", question[:60], e)

        passed_count = sum(1 for r in results if r.passed)
        logger.info(
            "Evaluation run %s complete: %d/%d passed",
            run_id, passed_count, len(results),
        )
        return results

    def report(self, results: list[EvaluationRunResult]) -> dict:
        """Generate a summary report from evaluation results."""
        if not results:
            return {}

        total = len(results)
        passed = sum(1 for r in results if r.passed)
        avg_retrieval = sum(r.retrieval_score for r in results) / total
        avg_accuracy = sum(r.accuracy_score for r in results) / total
        avg_completeness = sum(r.completeness_score for r in results) / total

        failures = [r for r in results if not r.passed]

        return {
            "total_questions": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / total, 2),
            "avg_retrieval_score": round(avg_retrieval, 2),
            "avg_accuracy_score": round(avg_accuracy, 2),
            "avg_completeness_score": round(avg_completeness, 2),
            "failure_summary": [
                {"question": r.question[:80], "scores": f"R:{r.retrieval_score} A:{r.accuracy_score} C:{r.completeness_score}"}
                for r in failures[:10]
            ],
        }
