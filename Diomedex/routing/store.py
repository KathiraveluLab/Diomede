destinations = {}
def add_destination(name: str, config: dict):
    if name in destinations: raise ValueError(f"Destination '{name}' already exists")
    destinations[name] = config
def remove_destination(name: str):
    if name not in destinations: raise KeyError(name)
    del destinations[name]
def update_destination(name: str, updates: dict):
    config = destinations[name]
    for key, value in updates.items():
        if key in config: config[key] = value
def get_all_destinations():
    return destinations
