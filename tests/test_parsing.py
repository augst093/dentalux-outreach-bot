from app.bot.parsing import Cmd, detect_opt_out, parse_command, parse_wish_lang


def test_list_command_variants():
    assert parse_command("список кому пишешь").cmd == Cmd.LIST
    assert parse_command("  Список   кому   пишешь ").cmd == Cmd.LIST


def test_yes_no_are_exact_words():
    assert parse_command("Да").cmd == Cmd.APPROVE
    assert parse_command("нет").cmd == Cmd.SKIP
    assert parse_command("да, конечно").cmd == Cmd.UNKNOWN


def test_redo_with_and_without_wish():
    parsed = parse_command("Переделай: короче и без восклицаний")
    assert parsed.cmd == Cmd.REDO
    assert parsed.text == "короче и без восклицаний"
    assert parse_command("переделай").text is None


def test_reply_keeps_name_and_text():
    parsed = parse_command("ответ Dental Plus: Спасибо, пришлите подробности: цены?")
    assert parsed.cmd == Cmd.REPLY
    assert parsed.name == "Dental Plus"
    assert parsed.text == "Спасибо, пришлите подробности: цены?"


def test_reply_without_colon_gives_format_hint():
    parsed = parse_command("ответ Dental Plus спасибо")
    assert parsed.cmd == Cmd.REPLY
    assert parsed.error and "ответ <клиника>" in parsed.error


def test_payment_and_opt_out_commands():
    prepay = parse_command("предоплата Dental Plus: 250$")
    assert prepay.cmd == Cmd.PREPAY
    assert prepay.name == "Dental Plus"
    assert prepay.amount == "250$"
    assert parse_command("opt_out Dental Plus").name == "Dental Plus"
    assert parse_command("optout Dental Plus").cmd == Cmd.OPT_OUT


def test_status_sent_negotiate_launch_commands():
    assert parse_command("статус").cmd == Cmd.STATUS
    assert parse_command("отправлено Dental Plus").name == "Dental Plus"
    assert parse_command("переговоры Dental Plus").cmd == Cmd.NEGOTIATE
    launch = parse_command("запуск Dental Plus")
    assert launch.cmd == Cmd.LAUNCH and launch.amount is None
    assert parse_command("запуск Dental Plus: 200").amount == "200"


def test_unknown_text():
    assert parse_command("привет").cmd == Cmd.UNKNOWN


def test_wish_language_switch():
    assert parse_wish_lang("на узбекском, коротко") == "uz"
    assert parse_wish_lang("сделай по-русски") == "ru"
    assert parse_wish_lang("сделай короче") is None
    assert parse_wish_lang("на русском") == "ru"
    assert parse_wish_lang(None) is None


def test_opt_out_detection_positive():
    for text in [
        "Не пишите нам больше, спасибо",
        "Пожалуйста, не беспокойте",
        "Отпишите нас от рассылки",
        "Stop",
        "Iltimos yozmang",
        "удалите наш номер",
    ]:
        assert detect_opt_out(text), text


def test_opt_out_detection_negative():
    for text in ["Спасибо, интересно, пришлите презентацию", "Да, давайте созвонимся в четверг"]:
        assert not detect_opt_out(text), text
