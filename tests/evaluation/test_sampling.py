from src.evaluation.evaluator import should_sample


def test_rate_zero_never_samples():
    assert should_sample(0.0, 0.0) is False
    assert should_sample(0.0, 0.999) is False


def test_rate_one_always_samples():
    assert should_sample(1.0, 0.0) is True
    assert should_sample(1.0, 0.999) is True


def test_draw_below_rate_samples():
    assert should_sample(0.1, 0.05) is True


def test_draw_at_or_above_rate_does_not_sample():
    assert should_sample(0.1, 0.1) is False
    assert should_sample(0.1, 0.5) is False
