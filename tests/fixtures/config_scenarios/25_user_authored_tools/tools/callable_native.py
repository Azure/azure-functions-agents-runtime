from datetime import date


class Lookup:
    name = "callable_lookup"
    description = "Named authored callable"

    def __call__(self, value: str) -> str:
        raise AssertionError("Discovery must not invoke authored callables")


lookup = Lookup()
unrelated = date(2026, 10, 14)
