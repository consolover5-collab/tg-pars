from bot.keywords import KeywordMatcher, _stem


def test_stem_strips_trailing_vowel():
    assert _stem("колонка") == "колонк"


def test_stem_keeps_short_and_latin():
    assert _stem("тв") == "тв"
    assert _stem("iphone") == "iphone"


def test_match_inflected_forms():
    m = KeywordMatcher(["колонка"])
    assert m.match("Продам колонку JBL") == "колонк"
    assert m.match("отдам колонки") == "колонк"
    assert m.match("продам стол") is None


def test_synonyms_resolve_to_key():
    m = KeywordMatcher([], keyword_map={"колонка": ["акустика", "саундбар"]})
    hit = m.match("Продаю акустику Sony")
    assert hit is not None
    assert m.resolve_key(hit) == "колонка"


def test_empty_matcher_matches_nothing():
    m = KeywordMatcher([])
    assert m.match("что угодно") is None
