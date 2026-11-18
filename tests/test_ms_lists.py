import pytest

from bharat_courts.integrations.ms_lists import (
    lookup_field,
    normalize_field_map,
    parse_lists_url,
)


def test_parse_lists_url_valid():
    url = "https://lists.live.com/?listId=2448d6908f6a4d75af37cc138991094e%5Ff5b969a95d74bb55"
    guid, user_id = parse_lists_url(url)
    assert guid == "2448d690-8f6a-4d75-af37-cc138991094e"
    assert user_id == "f5b969a95d74bb55"


def test_parse_lists_url_with_dashes():
    url = "https://lists.live.com/?listId=2448d690-8f6a-4d75-af37-cc138991094e_f5b969a95d74bb55"
    guid, user_id = parse_lists_url(url)
    assert guid == "2448d690-8f6a-4d75-af37-cc138991094e"
    assert user_id == "f5b969a95d74bb55"


def test_parse_lists_url_invalid():
    with pytest.raises(ValueError):
        parse_lists_url("https://lists.live.com/?listId=invalid")


def test_normalize_field_map():
    raw_fields = [
        {
            "Title": "Title",
            "InternalName": "Title",
            "EntityPropertyName": "Title",
            "TypeAsString": "Text",
        },
        {
            "Title": "Parties",
            "InternalName": "LinkTitle",
            "EntityPropertyName": "LinkTitle",
            "TypeAsString": "Computed",
        },
        {
            "Title": "Next Hearing",
            "InternalName": "Date",
            "EntityPropertyName": "Date",
            "TypeAsString": "DateTime",
        },
        {
            "Title": "Case No",
            "InternalName": "PartyName",
            "EntityPropertyName": "PartyName",
            "TypeAsString": "Text",
        },
        {
            "Title": "CNR No",
            "InternalName": "CNRNo",
            "EntityPropertyName": "CNRNo",
            "TypeAsString": "Text",
        },
        {
            "Title": "Court",
            "InternalName": "Court",
            "EntityPropertyName": "Court",
            "TypeAsString": "Text",
        },
    ]
    mapping = normalize_field_map(raw_fields)
    assert mapping["parties"] == "Title"
    assert mapping["next hearing"] == "Date"
    assert mapping["case no"] == "PartyName"
    assert mapping["cnr no"] == "CNRNo"
    assert mapping["court"] == "Court"


def test_lookup_field():
    field_map = {
        "title": "Title",
        "next hearing": "Next_x0020_Hearing",
        "case no": "Case_x0020_No",
        "cnr no": "CNR_x0020_No",
        "court": "Court",
    }
    assert lookup_field(field_map, ["parties", "title"], "Title") == "Title"
    assert (
        lookup_field(field_map, ["next hearing", "next hearing date"], "fallback")
        == "Next_x0020_Hearing"
    )
    assert lookup_field(field_map, ["case no", "case number"], "fallback") == "Case_x0020_No"
    assert lookup_field(field_map, ["cnr no", "cnr"], "fallback") == "CNR_x0020_No"
    assert lookup_field(field_map, ["court"], "fallback") == "Court"
    assert lookup_field(field_map, ["nonexistent"], "fallback") == "fallback"


def test_personal_lists_client_init():
    from bharat_courts.integrations.ms_lists import PersonalListsClient

    url = "https://lists.live.com/?listId=2448d6908f6a4d75af37cc138991094e%5Ff5b969a95d74bb55"
    client = PersonalListsClient(list_url=url, force_login=True)
    assert client.force_login is True
    assert client.list_guid == "2448d690-8f6a-4d75-af37-cc138991094e"
    assert client.user_id == "f5b969a95d74bb55"
    assert client.state_file.name == "storage_state.json"
