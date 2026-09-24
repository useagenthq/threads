"""An extension with no hooks or tools needs no type argument: pyright strict (which checks this
file) infers Extension[None] even without context, so passing it to agent() is not an unknown
argument."""

from threads import agent, extension, scripted_model

STYLE = extension(name="style", instructions="Answer briefly.")


def test_an_extension_with_only_instructions_types_as_extension_none() -> None:
    bot = agent(model=scripted_model({"responses": []}), extensions=[STYLE])
    assert bot.definition.extensions[0].name == "style"
