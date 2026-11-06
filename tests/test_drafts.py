import pytest

from app.services.drafts import MAX_LENGTH, DraftGenerator, DraftRequest, validate_draft

REQ = DraftRequest(clinic_name="Dental Plus", direction="импланты", note="сайт: импланты и виниры")


async def test_without_key_uses_template_and_says_so():
    draft = await DraftGenerator(api_key=None).generate(REQ)
    assert draft.source == "template"
    assert draft.note and "ANTHROPIC_API_KEY" in draft.note
    assert validate_draft(draft.text, lang="ru") == []
    assert "DentaLux" in draft.text and "ассистент" in draft.text


@pytest.mark.parametrize("lang", ["ru", "uz"])
@pytest.mark.parametrize("kind", ["outreach", "reply"])
async def test_every_template_passes_validation(lang, kind):
    req = DraftRequest(
        clinic_name="Clinic with a long name " * 3,
        direction="брекеты и виниры",
        note=None,
        lang=lang,
        kind=kind,
        clinic_reply="спасибо",
    )
    draft = await DraftGenerator(api_key=None).generate(req)
    assert validate_draft(draft.text, lang=lang, kind=kind) == [], draft.text


async def test_valid_api_output_is_used():
    async def llm(system, user):
        return (
            "Здравствуйте. Это ассистент DentaLux, мы помогаем клиникам с онлайн-записью. "
            "Увидели ваши импланты на сайте. Вам было бы интересно посмотреть 3-минутный просмотр?"
        )

    draft = await DraftGenerator(llm=llm).generate(REQ)
    assert draft.source == "claude"
    assert draft.note is None


async def test_invalid_api_output_falls_back_to_template():
    async def llm(system, user):
        return "Супер скидки 30%! Гарантируем рост выручки. Пишите сейчас!"

    draft = await DraftGenerator(llm=llm).generate(REQ)
    assert draft.source == "template"
    assert "validator" not in draft.text
    assert validate_draft(draft.text, lang="ru") == []


async def test_api_exception_never_crashes_and_falls_back():
    async def llm(system, user):
        raise ConnectionError("network down")

    draft = await DraftGenerator(llm=llm).generate(REQ)
    assert draft.source == "template"
    assert draft.note and "недоступен" in draft.note


async def test_prompt_contains_owner_wish_and_previous_text():
    seen = {}

    async def llm(system, user):
        seen["user"] = user
        return "Здравствуйте. Это ассистент DentaLux. Посмотрите просмотр на 3 минуты?"

    req = DraftRequest(clinic_name="X", direction=None, note=None, instruction="короче", previous_text="старый текст")
    await DraftGenerator(llm=llm).generate(req)
    assert "короче" in seen["user"] and "старый текст" in seen["user"]


def test_validator_rejects_each_rule_violation():
    good = "Здравствуйте. Это ассистент DentaLux, работаем с клиниками. Интересен короткий демо-звонок?"
    assert validate_draft(good, lang="ru") == []
    assert validate_draft(good.replace(".", "!", 1), lang="ru")  # exclamation
    assert validate_draft(good + " Ещё вопрос?", lang="ru")  # two questions
    assert validate_draft(good.replace("DentaLux", "Компания"), lang="ru")  # no product name
    assert validate_draft(good.replace("ассистент", "менеджер"), lang="ru")  # not disclosed as assistant
    assert validate_draft(good.replace("Здравствуйте.", "ПРИВЕТ ВСЕМ."), lang="ru")  # caps
    assert validate_draft(good + " Гарантируем выручку?", lang="ru")  # revenue promise
    assert validate_draft(good + " Цена 300 $?", lang="ru")  # price not from the brief
    assert validate_draft("x" * (MAX_LENGTH + 1) + "?", lang="ru")  # too long


def test_outreach_must_not_contain_prices_even_brief_ones():
    text = "Здравствуйте. Это ассистент DentaLux. Стоит 250 $ предоплата, интересно обсудить?"
    assert any("цен" in p for p in validate_draft(text, lang="ru", kind="outreach"))
    assert validate_draft(text, lang="ru", kind="reply") == []


def test_uzbek_marker_is_checked():
    text = "Assalomu alaykum. Men DentaLux yordamchisiman. Qisqa demo ko'rishni xohlaysizmi?"
    assert validate_draft(text, lang="uz") == []
    assert validate_draft(text.replace("DentaLux", "Zuzu"), lang="uz")
