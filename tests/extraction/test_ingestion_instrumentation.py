from src.config import Settings
from src.extraction.graph_extractor import GraphExtractor
from src.extraction.sql_extractor import SQLExtractor
from src.ingestion import classifier as ing_classifier
from src.ingestion.parsers import vision_parser


def test_graph_extractor_client_call_site():
    g = GraphExtractor(Settings(anthropic_api_key="x"))
    assert g.client.call_site == "ingestion.graph_extractor"


def test_sql_extractor_client_call_site():
    s = SQLExtractor(Settings(anthropic_api_key="x"))
    assert s.client.call_site == "ingestion.sql_extractor"


def test_ingestion_classifier_make_llm_call_site():
    llm = ing_classifier._make_llm(Settings(anthropic_api_key="x"))
    assert llm.call_site == "ingestion.classifier"


def test_vision_parser_make_llm_call_site():
    llm = vision_parser._make_llm(Settings(anthropic_api_key="x"))
    assert llm.call_site == "ingestion.vision_parser"
