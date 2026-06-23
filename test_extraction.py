"""Tests for the multi-signal (email + audio + image) claude extraction in main."""
import main


def test_parse_extraction_normalises_claude_json():
    reply = (
        '{"supplier_name": "Dreamland", "parcel_count": "30", '
        '"unit": "colis", "goods_type": "frais", "has_damage": false}'
    )
    fields = main.parse_extraction(reply)
    assert fields["supplier_name"] == "Dreamland"
    assert fields["parcel_count"] == 30
    assert fields["unit"] == "parcels"          # colis -> parcels
    assert fields["goods_type"] == "perishable"  # frais -> perishable
    assert fields["has_damage"] is False


def test_parse_extraction_reads_fenced_reply_and_damage():
    reply = (
        "Here you go:\n```json\n"
        '{"supplier_name": "ACME", "parcel_count": 5, "unit": "pallets", '
        '"goods_type": "oversized", "has_damage": true}\n```'
    )
    fields = main.parse_extraction(reply)
    assert fields["supplier_name"] == "ACME"
    assert fields["unit"] == "pallets"
    assert fields["goods_type"] == "oversized"
    assert fields["has_damage"] is True


def test_build_extraction_prompt_includes_all_three_signals():
    prompt = main.build_extraction_prompt(
        {
            "email": "Bonjour, livraison standard",
            "transcript": "trente colis de Dreamland",
            "image_description": "boxes look intact",
        }
    )
    assert "Bonjour, livraison standard" in prompt
    assert "trente colis de Dreamland" in prompt
    assert "boxes look intact" in prompt


def test_describe_image_is_a_stub_returning_empty():
    # Image vision is added later; for now the stub yields no description.
    assert main.describe_image("https://example.com/TRK-001.jpg") == ""


def test_extract_truck_combines_signals_and_maps_supplier_afterwards():
    msg = {
        "truck_id": "TRK-001",
        "priority": "high",
        "documentation": [
            {"type": "email", "text": "Bonjour"},
            {"type": "audio", "url": "/assets/audio/TRK-001.mp3"},
            {"type": "photo", "url": "/assets/photo/TRK-001.jpg"},
        ],
    }
    seen = {}

    def fake_claude(prompt: str) -> str:
        seen["prompt"] = prompt
        # claude returns only a most-likely supplier NAME, never an id
        return (
            '{"supplier_name": "Dreamland", "parcel_count": 30, '
            '"unit": "parcels", "goods_type": "standard", "has_damage": false}'
        )

    fields = main.extract_truck(
        msg,
        transcribe_fn=lambda url: "trente colis de Dreamland",
        describe_fn=lambda url: "intact boxes",
        claude_fn=fake_claude,
    )

    # every signal reached the extraction model
    assert "Bonjour" in seen["prompt"]
    assert "trente colis de Dreamland" in seen["prompt"]
    assert "intact boxes" in seen["prompt"]

    # supplier-id mapping happens AFTER extraction, from the returned name
    assert fields["supplier_id"] == 1005981
    assert fields["supplier_name"] == "Dreamland"
    assert fields["parcel_count"] == 30
    assert fields["unit"] == "parcels"
    assert fields["has_damage"] is False
    assert fields["goods_type"] == "standard"
