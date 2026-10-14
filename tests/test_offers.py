"""Tests for YouDo offer submission safety guards and payload generation."""
import pytest
from youdo_monitor.offers import (
    create_postpaid_payload,
    SAFETY_POSTPAYMENT_TYPE,
    is_postpaid_available_in_dom,
    OfferManager,
)


def test_safety_guard_constant():
    # Postpayment type on YouDo MUST be 4
    assert SAFETY_POSTPAYMENT_TYPE == 4


def test_create_postpaid_payload():
    payload = create_postpaid_payload(
        task_id=15290010,
        price=8500,
        description="Тестовый отклик"
    )

    assert payload["offer"]["TaskId"] == 15290010
    assert payload["offer"]["Price"] == "8500"
    assert payload["offer"]["Description"] == "Тестовый отклик"
    assert payload["offer"]["ContactRequestAutoAccept"] is True
    assert payload["paymentData"]["paymentType"] == 4


def test_create_payload_rejects_invalid_payment_type():
    with pytest.raises(ValueError, match="SAFETY GUARD"):
        create_postpaid_payload(
            task_id=15290010,
            price=8500,
            description="Тестовый отклик",
            payment_type=1  # prepaid must be rejected!
        )


def test_is_postpaid_available_in_dom():
    dom_with_postpay = """
    <div class="TaskActions_actions__abc">
        <a class="TaskActions_button__un7jc TaskActions_iPerform__7xLIJ" data-test="taskActionAddOffer">Откликнуться за 142 Р</a>
        <a class="TaskActions_button__un7jc TaskActions_iPostPayment__R6gru" data-test="taskActionAddPostPayment">Откликнуться бесплатно отклик — 0 Р</a>
    </div>
    """
    assert is_postpaid_available_in_dom(dom_with_postpay) is True

    dom_without_postpay = """
    <div class="TaskActions_actions__abc">
        <a class="TaskActions_button__un7jc TaskActions_iPerform__7xLIJ" data-test="taskActionAddOffer">Откликнуться за 142 Р</a>
    </div>
    """
    assert is_postpaid_available_in_dom(dom_without_postpay) is False

    dom_already_offered = """
    <div class="TaskActions_actions__abc">
        <button class="ButtonGhost_button__ELkOp">Удалить отклик</button>
    </div>
    """
    assert is_postpaid_available_in_dom(dom_already_offered) is False


def test_dom_forecast_phrase_is_not_treated_as_existing_offer():
    """Regression: «Ваш отклик окажется на 10 месте…» is a placement forecast.

    It renders on tasks we have NOT answered. Treating it as proof of an
    existing offer silently skipped every real auto-offer.
    """
    dom_not_answered_yet = """
    <div class="TaskActions_actions__abc">
        <a data-test="taskActionAddOffer">Откликнуться за 142 Р</a>
        <a data-test="taskActionAddPostPayment">Откликнуться бесплатно</a>
        <div class="TaskActions_forecast">Ваш отклик окажется на 10 месте, если вы ответите сейчас</div>
    </div>
    """
    assert is_postpaid_available_in_dom(dom_not_answered_yet) is True
