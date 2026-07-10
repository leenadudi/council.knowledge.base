from src.ingestion.quality import text_readability, is_garbled

# Real captured text from a good resolution (readable)
_GOOD = (
    "WHEREAS, the City of Harrisburg City Council wishes to authorize the Mayor "
    "to enter into a lease agreement with the vendor; and WHEREAS the term shall "
    "be for a period of three years; NOW THEREFORE BE IT RESOLVED that the "
    "Council of the City of Harrisburg hereby approves the said agreement."
)
# Real captured gibberish from Res 19's bad OCR layer
_BAD = (
    "Yy Aavos AouoH feuoneN yd eddey nig No AYaId0S 1OU0H ssauisng euisis Bute "
    "eleg ZIG sscuoH jeuoQeUeyUy ASOVV ey Aq pezpes9e AyjeuoneEU ssautsng jo "
    "asayjoy NL oyTAaxyoo9 AUSIOAIUL YDI vossautiay suQuno2oy Ul YoMas.nod"
)


def test_good_text_scores_high():
    assert text_readability(_GOOD) > 0.5


def test_gibberish_scores_low():
    assert text_readability(_BAD) < 0.2


def test_is_garbled_true_for_gibberish():
    assert is_garbled(_BAD) is True


def test_is_garbled_false_for_good_text():
    assert is_garbled(_GOOD) is False


def test_empty_text_is_garbled():
    assert is_garbled("") is True
