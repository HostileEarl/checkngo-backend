# notifications/tests/test_messages.py
from dataclasses import dataclass

from notifications.messages import render_invitation_message, render_low_stock_message
from notifications.sms import SMS_SEGMENT_LIMIT, is_gsm7_basic


@dataclass
class FakeItem:
    """Stand-in for an InventoryItem — only .id and .name are used."""

    id: int
    name: str


class TestRenderInvitationMessage:
    def test_typical_length_fits_one_segment(self):
        message = render_invitation_message("Santos Layer Farm", "483920")
        assert len(message) <= SMS_SEGMENT_LIMIT
        assert "483920" in message

    def test_worst_case_60_char_farm_name_still_fits_one_segment(self):
        farm_name = "A" * 60
        message = render_invitation_message(farm_name, "483920")
        assert len(message) <= SMS_SEGMENT_LIMIT
        # The PIN is never shortened or altered.
        assert "483920" in message
        assert message.count("483920") == 1

    def test_pin_is_never_altered_even_when_farm_name_is_truncated(self):
        farm_name = "X" * 200
        pin = "007911"
        message = render_invitation_message(farm_name, pin)
        assert len(message) <= SMS_SEGMENT_LIMIT
        assert f"PIN is {pin}." in message

    def test_curly_apostrophe_and_n_tilde_farm_name_is_sanitized_and_fits(self):
        farm_name = "Peña’s Farm"
        message = render_invitation_message(farm_name, "483920")
        assert is_gsm7_basic(message)
        assert len(message) <= SMS_SEGMENT_LIMIT
        assert "Peña's Farm" in message


class TestRenderLowStockMessage:
    def test_typical_length_fits_one_segment(self):
        items = [FakeItem(1, "Feed Sacks"), FakeItem(2, "Disinfectant")]
        message, context_key = render_low_stock_message("Santos Layer Farm", items)
        assert len(message) <= SMS_SEGMENT_LIMIT
        assert "Feed Sacks" in message
        assert "Disinfectant" in message
        assert context_key == "1,2"

    def test_worst_case_long_farm_name_and_ten_long_items_fits_one_segment(self):
        farm_name = "Santos and Sons Integrated Poultry Layer Operations Corporation"
        assert len(farm_name) > 60
        items = [
            FakeItem(i, f"Premium Grower Feed Concentrate Sack Type {i:02d}")
            for i in range(1, 11)
        ]

        message, context_key = render_low_stock_message(farm_name, items)

        assert len(message) <= SMS_SEGMENT_LIMIT
        assert is_gsm7_basic(message)
        # The farm name is capped, not the item count silently dropped —
        # "and N more" must show the true number left out.
        assert "and " in message and " more" in message
        assert context_key == ",".join(str(i) for i in range(1, 11))

    def test_and_n_more_count_is_accurate(self):
        items = [FakeItem(i, f"Item{i}") for i in range(1, 4)]
        message, _ = render_low_stock_message("Farm", items)
        # All three short names should fit without truncation.
        assert "Item1" in message and "Item2" in message and "Item3" in message
        assert "more" not in message

    def test_farm_name_capped_at_30_characters(self):
        items = [FakeItem(1, "Feed")]
        farm_name = "Z" * 60
        message, _ = render_low_stock_message(farm_name, items)
        assert "Z" * 31 not in message
        assert "Z" * 30 in message

    def test_never_drops_items_silently_when_count_is_shown(self):
        """
        Every item that fits is shown in full (never partially cut); any
        left over are counted, never silently omitted with no trace.
        """
        items = [FakeItem(i, f"Very Long Inventory Item Name Number {i:03d}") for i in range(1, 11)]
        message, context_key = render_low_stock_message("Farm", items)

        assert len(message) <= SMS_SEGMENT_LIMIT
        shown_names = [i.name for i in items if i.name in message]
        remaining = len(items) - len(shown_names)
        if remaining:
            assert f"and {remaining} more" in message
        assert context_key == ",".join(str(i) for i in range(1, 11))

    def test_curly_apostrophe_and_n_tilde_in_item_and_farm_name(self):
        farm_name = "Peña’s Farm"
        items = [FakeItem(1, "Muñoz’s Starter Feed")]
        message, _ = render_low_stock_message(farm_name, items)
        assert is_gsm7_basic(message)
        assert len(message) <= SMS_SEGMENT_LIMIT
        assert "Peña's Farm" in message
        assert "Muñoz's Starter Feed" in message
