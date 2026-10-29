SETTINGS = {"SHOP_CONTRIBUTORS:append": ["reviews"]}


def settings(namespace):
    closure = namespace["COMPOSE_CLOSURE"]
    assert closure.dependencies["reviews"] == ("catalog",)
    return {"SHOP_SELECTED_APPS": tuple(entry.name for entry in closure.app_entries)}
