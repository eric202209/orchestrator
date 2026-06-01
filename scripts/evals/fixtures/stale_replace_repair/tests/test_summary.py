from stale_replace import render_inventory


def test_render_inventory_counts_items_in_sorted_order():
    result = render_inventory(["pear", "apple", "apple", "banana"])

    assert result.splitlines() == [
        "apple: 2",
        "banana: 1",
        "pear: 1",
    ]


def test_render_inventory_ignores_blank_names():
    assert render_inventory(["", "  ", "Apple", "apple"]) == "apple: 2"
