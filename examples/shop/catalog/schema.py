"""A deterministic SDL-like artifact, with no GraphQL runtime dependency."""


def schema(model):
    fields = "\n".join(f"  {field.name}: {field.get_internal_type()}" for field in model._meta.fields)
    return f"type Item {{\n{fields}\n}}\n".encode()
