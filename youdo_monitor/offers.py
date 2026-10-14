"""YouDo offer submission management with strict post-payment safety guards."""
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("youdo_monitor.offers")

# SAFETY INVARIANT: YouDo paymentType == 4 represents post-payment (pay-on-contact).
# Submitting with paymentType == 1 charges upfront balance immediately.
# We NEVER allow paymentType != 4.
SAFETY_POSTPAYMENT_TYPE = 4


def create_postpaid_payload(
    task_id: int,
    price: int,
    description: str,
    payment_type: int = SAFETY_POSTPAYMENT_TYPE
) -> Dict[str, Any]:
    """Build the exact JSON payload for a post-paid offer. Raises ValueError on any non-postpaid type."""
    if payment_type != SAFETY_POSTPAYMENT_TYPE:
        raise ValueError(
            f"SAFETY GUARD: Refusing non-postpaid offer! paymentType must be {SAFETY_POSTPAYMENT_TYPE}, got {payment_type}."
        )

    return {
        "offer": {
            "ActualTime": None,
            "Description": description,
            "ExecutorCardId": None,
            "ExecutorCardMask": None,
            "IsSbr": False,
            "Price": str(price),
            "TaskId": int(task_id),
            "ContactRequestAutoAccept": True,
        },
        "paymentData": {
            "paymentType": SAFETY_POSTPAYMENT_TYPE
        }
    }


def is_postpaid_available_in_dom(html: str) -> bool:
    """Check if the post-payment submission button is present and active in raw HTML."""
    if not html:
        return False
    if "Удалить отклик" in html:
        return False  # Already applied
    return (
        'data-test="taskActionAddPostPayment"' in html
        or "TaskActions_iPostPayment__R6gru" in html
        or "Откликнуться бесплатно" in html
    )


class OfferManager:
    """Manages offer submission for YouDo tasks through Playwright session."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def check_postpaid_status(self, page: Any, task_id: int) -> Dict[str, Any]:
        """Inspect task page to verify whether a post-paid offer can be submitted."""
        try:
            url = f"https://youdo.com/t{task_id}"
            page.goto(url, wait_until="domcontentloaded", timeout=35000)
            page.wait_for_timeout(2000)

            status = page.evaluate("""() => {
                const bodyText = document.body.innerText || '';
                const postBtn = document.querySelector('[data-test="taskActionAddPostPayment"]')
                    || document.querySelector('.TaskActions_iPostPayment__R6gru');
                const prepayBtn = document.querySelector('[data-test="taskActionAddOffer"]');

                // Ground truth: if NEITHER offer button exists, our offer is
                // already there (the page shows «Удалить отклик» instead).
                //
                // Do NOT infer this from the phrase «Ваш отклик»: the page
                // renders «Ваш отклик окажется на 10 месте, если вы …» as a
                // placement forecast on tasks we have NOT answered, which made
                // the guard skip a real auto-offer. Button presence is the
                // only reliable signal.
                const alreadyOffered = !postBtn && !prepayBtn;

                return {
                    already_offered: Boolean(alreadyOffered),
                    delete_button_visible: bodyText.includes('Удалить отклик'),
                    postpaid_available: Boolean(postBtn),
                    prepay_available: Boolean(prepayBtn),
                    button_text: postBtn ? postBtn.innerText.trim().replace(/\\s+/g, ' ') : null
                };
            }""")
            return status
        except Exception as e:
            logger.warning("Error checking postpaid status for task %s: %s", task_id, e)
            return {
                "already_offered": False,
                "postpaid_available": False,
                "prepay_available": False,
                "error": str(e)
            }

    def submit_postpaid_offer(
        self,
        client: Any,
        task_id: int,
        price: int,
        description: str
    ) -> Dict[str, Any]:
        """Submit a post-paid offer (paymentType == 4, 0 RUB upfront) via YouDo internal API."""
        payload = create_postpaid_payload(task_id, price, description)

        if self.dry_run:
            logger.info("[DRY-RUN] Would submit postpaid offer for task %s: %s", task_id, payload)
            return {
                "success": True,
                "task_id": task_id,
                "price": price,
                "dry_run": True,
                "payload": payload
            }

        page = getattr(client, "_page", None)
        if not page:
            return {"success": False, "error": "Client page not initialized"}

        # Verify postpayment is actually available on this task page before posting
        check = self.check_postpaid_status(page, task_id)
        if not check.get("postpaid_available"):
            if check.get("already_offered"):
                reason = "already_offered"
            else:
                reason = "postpayment_unavailable"
            logger.warning("Task %s: skipping postpaid offer (%s).", task_id, reason)
            return {"success": False, "reason": reason}

        # Execute API call from page context inheriting all cookies & challenges
        try:
            js_code = """
            async (p) => {
                const r = await fetch('/api/offers/save/', {
                    method: 'POST',
                    headers: {
                        'X-Requested-With': 'XMLHttpRequest',
                        'Accept': 'application/json',
                        'Content-Type': 'application/json'
                    },
                    credentials: 'include',
                    body: JSON.stringify(p)
                });
                const j = await r.json();
                return {
                    status: r.status,
                    ok: r.ok,
                    data: j
                };
            }
            """
            res = page.evaluate(js_code, payload)
            logger.info("Offer API response for task %s: %s", task_id, res)

            data = res.get("data") or {}
            is_success = bool(res.get("ok") and (data.get("IsSuccess") or data.get("ResultObject")))

            if is_success:
                logger.info("Successfully submitted postpaid offer for task %s (price %d ₽)", task_id, price)
                return {
                    "success": True,
                    "task_id": task_id,
                    "price": price,
                    "response": data
                }
            else:
                err_msg = data.get("Message") or res.get("status") or "API error"
                logger.error("Failed to submit postpaid offer for task %s: %s", task_id, err_msg)
                return {
                    "success": False,
                    "task_id": task_id,
                    "error": str(err_msg),
                    "response": data
                }
        except Exception as e:
            logger.exception("Exception submitting offer for task %s: %s", task_id, e)
            return {"success": False, "task_id": task_id, "error": str(e)}
