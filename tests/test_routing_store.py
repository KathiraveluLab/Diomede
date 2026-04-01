import pytest
from Diomedex.routing.store import destinations, add_destination, remove_destination, update_destination, get_all_destinations

def test_add_destination_works():
    destinations.clear(); add_destination("dest1", {"host": "a", "port": 104})
    assert get_all_destinations()["dest1"]["host"] == "a"

def test_add_destination_duplicate_fails():
    destinations.clear(); add_destination("dest1", {"host": "a"})
    with pytest.raises(ValueError): add_destination("dest1", {"host": "b"})

def test_remove_destination_works():
    destinations.clear(); add_destination("dest1", {"host": "a"}); remove_destination("dest1")
    assert "dest1" not in get_all_destinations()

def test_update_destination_works():
    destinations.clear(); add_destination("dest1", {"host": "a", "port": 104}); update_destination("dest1", {"host": "b", "ae": "x"})
    assert get_all_destinations()["dest1"] == {"host": "b", "port": 104}
