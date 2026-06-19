from triage.component.catwalk.metrics import fpr


def test_fpr():
    predictions_binary = [1, 1, 1, 0, 0, 0, 0, 0]
    labels = [1, 1, 0, 1, 0, 0, 0, 1]

    result = fpr([], predictions_binary, labels, [])
    # false positives = 1
    # total negatives = 4
    assert result == 0.25
