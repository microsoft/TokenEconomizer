"""A router's model variant is priced as its model.

A router names a model by `provider/model`, and OpenRouter adds a variant after a colon:
`openai/gpt-4:free`, `:nitro`, `:floor`, `:online` ("Supports variant suffixes (e.g.
openai/gpt-4:free)", OpenRouter's SDK documentation). The variant chooses how the request is
routed or billed, not which model runs, so its token price ratios are the model's."""
import pytest

from tokeneconomizer import pricing
from tokeneconomizer.plugin import accounting

USAGE = {"input_tokens": 100, "cache_read_input_tokens": 10_000,
         "cache_creation_input_tokens": 0, "output_tokens": 10}


@pytest.mark.parametrize("routed, model", [
    ("openai/gpt-6.1-sol:nitro", "gpt-6.1-sol"), ("openai/gpt-6.1-sol:floor", "gpt-6.1-sol"),
    ("google/gemini-4-argon:free", "gemini-4-argon"), ("openai/gpt-5.5:online", "gpt-5.5"),
    ("anthropic/claude-opus-5.5:thinking", "claude-opus-5.5"),
])
def test_a_variant_of_a_routed_model_is_priced_as_the_model(routed, model):
    assert pricing.resolve_model({"model": routed}) == model
    assert pricing.weights_for({"model": routed}) == pricing.weights_for({"model": model})
    assert accounting.effective(dict(USAGE, model=routed)) == accounting.effective(
        dict(USAGE, model=model))
    assert accounting.effective(dict(USAGE, model=routed))[1] == "model"


def test_an_id_that_is_not_routed_keeps_its_colon():
    # A colon outside a router's `provider/model` id is not a variant (`llama3:8b` is an Ollama
    # tag); such an id is unpriced either way, and stays as it is.
    assert pricing.resolve_model({"model": "llama3:8b"}) == "llama3:8b"


def test_an_unknown_routed_model_still_fails_loudly():
    with pytest.raises(ValueError):
        pricing.weights_for({"model": "acme/some-new-model:nitro"})
