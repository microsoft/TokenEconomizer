import warnings

from tokeneconomizer.microskills.observe import _label_failed


def test_label_failed_matches_the_labels_own_module_without_deprecation_warnings():
    # Python 3.13 deprecates passing re.split's maxsplit positionally.
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert _label_failed(["tests.app:Case"], "No module named 'tests'")
        assert not _label_failed(["tests/app.py"], "No module named 'pytest'")
