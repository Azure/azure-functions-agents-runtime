class Lookup:
    name = "callable_constructed"
    description = "Look up a value."

    def __call__(self, value: str) -> str:
        return value


lookup = Lookup()
