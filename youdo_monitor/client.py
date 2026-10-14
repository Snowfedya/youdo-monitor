"""Playwright-based client for interacting with YouDo internal APIs."""
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional
from playwright.sync_api import sync_playwright, BrowserContext, Page

from youdo_monitor.config import (
    PLAYWRIGHT_CHROME_BIN,
    PLAYWRIGHT_PROFILE_DIR,
    USER_AGENT,
    YOUDO_START_URL,
    FEED_PAGES_TO_FETCH,
    CATEGORIES,
    ONLY_VIRTUAL
)

logger = logging.getLogger(__name__)

# Chromium inherits the process proxy environment. This host exports
# HTTP_PROXY=http://127.0.0.1:8119 (the RKN-bypass egress relay), which routes
# Playwright out through a different IP than the VPS address. YouDo serves the
# "Доступ ограничен" block page on the relay egress and the real feed on the
# VPS egress, so the proxy vars must be cleared before the browser launches.
PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
    "NO_PROXY", "no_proxy",
)


def _scrub_proxy_env() -> List[str]:
    """Remove proxy env vars so Chromium egresses from the VPS address."""
    removed = []
    for var in PROXY_ENV_VARS:
        if os.environ.pop(var, None) is not None:
            removed.append(var)
    return removed



def extract_max_notification_id(panel_html: str) -> Optional[int]:
    """Return the largest ``data-id`` in the notification panel, else None.

    The panel marks every item with ``data-id`` (notification id) and
    ``data-object`` (task or user id). Only ``data-id`` counts: ``data-object``
    values are much smaller and would mark the wrong watermark.
    """
    ids = []
    for raw in re.findall(r'data-id="([^"]*)"', panel_html or ""):
        value = raw.strip()
        if value.isdigit() and len(value) >= 6:
            ids.append(int(value))
    return max(ids) if ids else None


def notification_dedup_key(note: Dict[str, Any]) -> str:
    """Build a stable dedup key for a scraped notification.

    The panel's ``data-id`` is the platform's permanent notification id and is
    the only stable identity: the rendered ``when`` is a RELATIVE label
    ("26 мин. назад" → "41 мин. назад" → "1 ч. назад"), so keying on it made
    every already-forwarded note look new on the next 15-minute cycle — the
    state file accumulated 23 keys for 8 distinct texts.
    """
    note_id = note.get("id")
    if note_id is not None and str(note_id).strip().isdigit():
        return f"id:{int(note_id)}"
    # No id in the DOM: fall back to the text alone. Two notifications with
    # identical text are the same event far more often than they are two
    # distinct ones, and the duplicate cost is lower than a re-forward.
    return f"text:{(note.get('text') or '').strip()}"


def parse_private_messages(payload: Dict[str, Any], peer: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten a ``getprivatemessages`` payload into incoming message dicts.

    ``Messages`` mixes both directions and carries no direction flag. In the
    live payload ``UserId`` is the AUTHOR and ``RecipientId`` the receiver, so
    an incoming message is one whose author is the dialog PEER — the object
    under ``ResultObject.Recipient`` (e.g. ``Id: 14316146`` for «Анна Ч.»),
    while ``Sender`` is us. Filtering on the wrong one would forward our own
    replies back as «a new message from the client».

    ``InformationMessages`` (platform service notices — «do not move to other
    messengers») is excluded: it is noise in a freelancer inbox and is
    already covered by the notification stream.
    """
    result_obj = payload.get("ResultObject") or {}
    peer_name = (peer or {}).get("UserName") or "Неизвестный"
    peer_id = (peer or {}).get("Id")
    out: List[Dict[str, Any]] = []
    for m in result_obj.get("Messages") or []:
        try:
            msg_id = int(m.get("Id"))
        except (TypeError, ValueError):
            continue
        if msg_id <= 0 or not (m.get("Text") or "").strip():
            continue
        author = m.get("UserId")
        if peer_id is not None and author is not None and author != peer_id:
            # Authored by us — our own outgoing reply, not an incoming event.
            continue
        out.append({
            "id": msg_id,
            "text": (m.get("Text") or "").strip(),
            "date": m.get("Date"),
            "sender_id": author,
            "sender_name": peer_name,
        })
    return out


class YouDoClient:
    """Handles communication with YouDo passing ServicePipe challenges via Playwright."""

    COOKIE_FILE = "/root/youdo-monitor/data/cookies_export.json"

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = None
        self._ctx: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._warmed_up = False

    @classmethod
    def _load_cookie_file(cls) -> List[Dict[str, Any]]:
        """Convert an exported cookie JSON (CDT/GetCookielTXT style) to the
        Playwright add_cookies() shape. Session cookies have no expiry."""
        try:
            with open(cls.COOKIE_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return []
        out = []
        for c in raw:
            item: Dict[str, Any] = {
                "name": c["name"],
                "value": c["value"],
                "domain": c["domain"],
                "path": c.get("path", "/"),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
            }
            same = c.get("sameSite", "")
            if same in ("strict", "Strict"):
                item["sameSite"] = "Strict"
            elif same in ("lax", "Lax"):
                item["sameSite"] = "Lax"
            elif same in ("no_restriction", "None"):
                item["sameSite"] = "None"
            if not c.get("session", False) and c.get("expirationDate"):
                item["expires"] = int(c["expirationDate"])
            out.append(item)
        return out

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def start(self) -> None:
        """Launch browser persistent context and warm up."""
        if self._ctx:
            return
        removed = _scrub_proxy_env()
        if removed:
            logger.info("Cleared proxy env vars for browser egress: %s", ", ".join(removed))
        logger.info("Launching Playwright Chromium persistent context...")
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            PLAYWRIGHT_PROFILE_DIR,
            headless=self.headless,
            executable_path=PLAYWRIGHT_CHROME_BIN,
            user_agent=USER_AGENT,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage"
            ]
        )
        cookies = self._load_cookie_file()
        if cookies:
            self._ctx.add_cookies(cookies)  # type: ignore[reportArgumentType]  # dict shape is accepted at runtime
            logger.info("Injected %d session cookies from %s", len(cookies), self.COOKIE_FILE)

        self._page = self._ctx.new_page()
        page = self._page
        logger.info("Warming up session on %s...", YOUDO_START_URL)
        page.goto(YOUDO_START_URL, wait_until="domcontentloaded", timeout=45000)
        for attempt in range(15):
            time.sleep(2)
            try:
                length = page.evaluate("document.body ? document.body.innerText.length : 0")
                if length > 2000:
                    logger.info("Session warmed up successfully (body len: %d)", length)
                    self._warmed_up = True
                    return
            except Exception as e:
                logger.debug("Warmup check error: %s", e)
        logger.warning("Session warmup timed out. Page may still be challenged.")
        self._warmed_up = False

    def close(self) -> None:
        """Safely close context and playwright runtime."""
        if self._ctx:
            try:
                self._ctx.close()
            except Exception as e:
                logger.debug("Error closing context: %s", e)
            self._ctx = None
            self._page = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception as e:
                logger.debug("Error stopping playwright: %s", e)
            self._pw = None

    def fetch_feed(self, categories: Optional[List[str]] = None, only_virtual: bool = ONLY_VIRTUAL, pages: int = FEED_PAGES_TO_FETCH) -> List[Dict[str, Any]]:
        """Fetch list of tasks across multiple pages via POST /api/tasks/tasks/."""
        if not self._page:
            raise RuntimeError("Client not started. Use with-statement or call start().")

        target_cats = categories or CATEGORIES
        all_items: Dict[int, Dict[str, Any]] = {}

        feed_js = """
        async ({page, cats, onlyVirtual}) => {
            const r = await fetch('/api/tasks/tasks/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': 'application/json, text/plain, */*'
                },
                body: JSON.stringify({
                    q: '', list: 'all', status: 'opened', radius: 50,
                    lat: 55.755864, lng: 37.617698, page, noOffers: false,
                    onlySbr: false, onlyB2B: false, onlyVacancies: false,
                    priceMin: '', sortType: 1, onlyVirtual, categories: cats
                })
            });
            const j = await r.json();
            const ro = j.ResultObject || {};
            return {
                total: ro.Total || 0,
                items: (ro.Items || []).map(it => ({
                    id: it.Id,
                    name: it.Name,
                    budget: it.BudgetDescription,
                    category: it.CategoryFlag,
                    url: it.Url,
                    date_string: it.DateTimeString,
                    is_b2b: it.IsB2B,
                    is_sbr: it.IsSbr,
                    creator_name: (it.CreatorInfo || {}).UserName,
                    creator_reviews: ((it.CreatorInfo || {}).Rating || {}).SummaryReviews
                }))
            };
        }
        """

        for pg in range(1, pages + 1):
            try:
                res = self._page.evaluate(feed_js, {"page": pg, "cats": target_cats, "onlyVirtual": only_virtual})
                items = res.get("items", [])
                logger.debug("Fetched page %d: %d items (total in list: %d)", pg, len(items), res.get("total", 0))
                for it in items:
                    if it["id"] not in all_items:
                        all_items[it["id"]] = it
                time.sleep(0.8)
            except Exception as e:
                logger.warning("Error fetching feed page %d: %s", pg, e)

        return list(all_items.values())

    def fetch_task_details(self, task_ids: List[int]) -> List[Dict[str, Any]]:
        """Fetch full details for tasks in batches via GET /api/tasks/task/{id}/."""
        if not self._page or not task_ids:
            return []

        detail_js = """
        async (ids) => {
            const out = [];
            for (const id of ids) {
                try {
                    const r = await fetch('/api/tasks/task/' + id + '/', {
                        headers: {
                            'X-Requested-With': 'XMLHttpRequest',
                            'Accept': 'application/json, text/plain, */*'
                        },
                        credentials: 'include'
                    });
                    const j = await r.json();
                    const td = (j.ResultObject || {}).TaskData || {};
                    out.push({
                        id,
                        description: td.Description || '',
                        offers_count: td.OffersCount,
                        is_in_progress: td.IsInProgress,
                        status_flag: (td.TaskStatus || {}).Flag,
                        is_virtual: td.IsVirtual,
                        is_b2b: td.IsB2B,
                        created_date: (td.Dates || {}).CreationDateTime,
                        category_name: (td.CategoryInfo || {}).Name,
                        subcategory_name: (td.SubcategoryInfo || {}).Name
                    });
                } catch (e) {
                    out.push({ id, error: String(e) });
                }
            }
            return out;
        }
        """

        results: List[Dict[str, Any]] = []
        batch_size = 10
        for i in range(0, len(task_ids), batch_size):
            chunk = task_ids[i:i + batch_size]
            try:
                batch_res = self._page.evaluate(detail_js, chunk)
                results.extend(batch_res)
                time.sleep(0.6)
            except Exception as e:
                logger.warning("Error fetching detail batch %s: %s", chunk, e)

        return results

    def check_auth_status(self) -> Dict[str, Any]:
        """Verify whether the current session is logged in.

        Detection is header-text based, not tag based: the "Войти" control is
        not consistently an <a>/<button>, so querying those tags reports a
        logged-in session on a guest profile.
        """
        if not self._page:
            return {"authenticated": False, "reason": "client_not_started"}
        try:
            return self._page.evaluate("""() => {
                const header = document.querySelector('header');
                const headerText = header ? header.innerText : '';
                const hasLogin = /(^|\\s)Войти(\\s|$)/.test(headerText);
                return {
                    authenticated: !hasLogin,
                    title: document.title,
                    headerText: headerText.slice(0, 200)
                };
            }""")
        except Exception as e:
            return {"authenticated": False, "error": str(e)}

    def fetch_account_counts(self) -> Dict[str, Any]:
        """Return unread notification/message counters.

        Both endpoints return ``{"ResultObject": <int>, "IsSuccess": true}``
        for an authenticated session and 404/403 for a guest session, so a
        guest run degrades to ``None`` values instead of failing.
        """
        if not self._page:
            raise RuntimeError("Client not started.")
        return self._page.evaluate("""async () => {
            const grab = async (u) => {
                try {
                    const r = await fetch(u, {credentials: 'include',
                        headers: {'X-Requested-With': 'XMLHttpRequest',
                                  'Accept': 'application/json, text/plain, */*'}});
                    if (!r.ok) return null;
                    const j = await r.json();
                    return (j && j.IsSuccess && j.ResultObject != null) ? j.ResultObject : null;
                } catch (e) { return null; }
            };
            return {
                notifications: await grab('/api/notifications/getnewnotificationsnumber/'),
                messages: await grab('/api/chat/getnewmessagesnumber/'),
            };
        }""")

    def fetch_notifications(self, limit: int = 15) -> List[Dict[str, str]]:
        """Read the notification dropdown rendered in the header.

        The panel is rendered from embedded page state rather than fetched
        over XHR, so it must be opened and scraped from the DOM. Items look
        like ``{"when": "вчера 21:35", "text": "Задание ... изменено."}``.
        """
        if not self._page:
            raise RuntimeError("Client not started.")
        try:
            self._page.click('[data-test="NotificationIcon"]', timeout=5000)
        except Exception as e:
            logger.warning("Could not open notification dropdown: %s", e)
            return []
        try:
            self._page.wait_for_function(
                """() => Array.from(document.querySelectorAll('div'))
                       .some(d => d.offsetParent && /^Уведомления/.test((d.innerText||'').trim()))""",
                timeout=5000,
            )
        except Exception as e:
            logger.warning("Notification dropdown did not render: %s", e)
            return []
        items = self._page.evaluate("""(limit) => {
            const cand = Array.from(document.querySelectorAll('div')).filter(
                x => x.offsetParent && /^Уведомления/.test((x.innerText||'').trim())
                     && (x.innerText||'').length < 3000);
            if (!cand.length) return [];
            const root = cand.reduce((a,b) => a.innerText.length > b.innerText.length ? a : b);
            const lines = root.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
            const out = [];
            let cur = null;
            for (const ln of lines) {
                // NB: no \\b here — JavaScript \\b is ASCII-only and does not
                // treat Cyrillic as a word char, so /^(сегодня|вчера)\\b/
                // never matches 'вчера 21:35'.
                const isStamp = /^(сегодня|вчера)(\\s|$)/i.test(ln) || /^\\d{1,2}\\s\\S+/.test(ln);
                if (isStamp) {
                    if (cur && cur.text) out.push(cur);
                    cur = {when: ln, text: ''};
                } else if (cur && !/^(Уведомления|Отметить все как прочитанные|Показать еще)$/.test(ln)) {
                    cur.text = cur.text ? cur.text + ' ' + ln : ln;
                }
            }
            if (cur && cur.text) out.push(cur);
            return out.slice(0, limit);
        }""", limit)
        try:
            self._page.keyboard.press("Escape")
        except Exception:
            pass
        return items

    PANEL_PREDICATE = """() => Array.from(document.querySelectorAll('div'))
       .some(d => d.offsetParent && /^Уведомления/.test((d.innerText||'').trim()))"""

    def _panel_is_open(self) -> bool:
        return bool(self._page and self._page.evaluate(self.PANEL_PREDICATE))

    def _open_notification_dropdown(self) -> bool:
        """Open the header notification panel. Returns True when it rendered.

        The bell TOGGLES the panel and Escape does not close it, so clicking
        blind would close an already-open panel. Check the state first.
        """
        if not self._page:
            return False
        if not self._panel_is_open():
            try:
                self._page.click('[data-test="NotificationIcon"]', timeout=5000)
            except Exception as e:
                logger.warning("Could not open notification dropdown: %s", e)
                return False
        try:
            self._page.wait_for_function(self.PANEL_PREDICATE, timeout=5000)
            return True
        except Exception as e:
            logger.warning("Notification dropdown did not render: %s", e)
            return False

    def mark_all_notifications_read(self) -> bool:
        """Clear the unread badge via YouDo's own «Отметить все как прочитанные».

        The endpoint is ``POST /api/notifications/markallnotificationsasvisited/``
        with ``{"maxId": <newest notification id>}``. ``maxId`` is a watermark
        the panel passes through React state, never over the wire, so it must be
        scraped from the panel's ``data-id`` attributes. Without it the POST is
        skipped — sending 0 would clear nothing.
        """
        if not self._page:
            raise RuntimeError("Client not started.")

        if not self._open_notification_dropdown():
            return False
        try:
            panel_html = self._page.evaluate(
                """() => {
                    const cands = Array.from(document.querySelectorAll('div')).filter(
                        x => x.offsetParent && /^Уведомления/.test((x.innerText||'').trim())
                             && (x.innerText||'').length < 3000);
                    if (!cands.length) return '';
                    return cands.reduce((a,b) =>
                        (a.innerText||'').length > (b.innerText||'').length ? a : b
                    ).outerHTML;
                }"""
            )
        finally:
            try:
                self._page.keyboard.press("Escape")
            except Exception:
                pass

        max_id = extract_max_notification_id(panel_html)
        if max_id is None:
            logger.info("No notification ids in panel — nothing to mark as read.")
            return False

        result = self._page.evaluate(
            """async (maxId) => {
                const r = await fetch('/api/notifications/markallnotificationsasvisited/', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                        'Accept': 'application/json, text/plain, */*'
                    },
                    credentials: 'include',
                    body: JSON.stringify({maxId})
                });
                return {ok: r.ok, status: r.status};
            }""",
            {"maxId": max_id},
        )
        if not result.get("ok"):
            logger.error("markallnotificationsasvisited failed: %s", result)
            return False
        logger.info("Marked notifications read up to id %d", max_id)
        return True

    def fetch_account_notifications(self) -> List[Dict[str, Any]]:
        """Entry point used by the runner: fetch account-level events."""
        if not self.check_auth_status().get("authenticated"):
            logger.info("Skipping account notifications: session is not logged in.")
            return []
        counts = self.fetch_account_counts()
        if not counts.get("notifications"):
            return []
        return self.fetch_notifications()

    def fetch_chat_messages(self, max_dialogs: int = 5) -> List[Dict[str, Any]]:
        """Return incoming chat messages from the most recent dialogs.

        Two endpoints, both verified against the live account:

        - ``GET /api/chat/getcontactlist/?newUserId=0`` — dialog list, newest
          first, each entry carrying ``SenderId`` (the peer) and
          ``IsNewMessage``.
        - ``GET /api/chat/getprivatemessages/?userId=<SenderId>&all=false`` —
          the conversation; ``ResultObject.Messages`` mixes both directions and
          ``ResultObject.Recipient`` identifies the peer.

        Only dialogs the platform itself flags as holding unread messages are
        opened: the counter endpoint is the cheap gate, and fetching every
        dialog every 15 minutes would re-read an unbounded amount of history
        to find nothing.
        """
        if not self._page:
            raise RuntimeError("Client not started.")

        dialogs = self._page.evaluate("""async () => {
            try {
                const r = await fetch('/api/chat/getcontactlist/?newUserId=0', {
                    credentials: 'include',
                    headers: {'X-Requested-With': 'XMLHttpRequest',
                              'Accept': 'application/json, text/plain, */*'}});
                if (!r.ok) return [];
                const j = await r.json();
                return (j && j.IsSuccess && Array.isArray(j.ResultObject))
                    ? j.ResultObject : [];
            } catch (e) { return []; }
        }""")
        unread = [d for d in dialogs if d.get("IsNewMessage")][:max_dialogs]
        if not unread:
            return []

        collected: List[Dict[str, Any]] = []
        for dialog in unread:
            peer_id = dialog.get("SenderId")
            if not peer_id:
                continue
            payload = self._page.evaluate(
                """async (userId) => {
                    try {
                        const r = await fetch(
                            `/api/chat/getprivatemessages/?userId=${userId}&all=false`, {
                            credentials: 'include',
                            headers: {'X-Requested-With': 'XMLHttpRequest',
                                      'Accept': 'application/json, text/plain, */*'}});
                        if (!r.ok) return null;
                        return await r.json();
                    } catch (e) { return null; }
                }""",
                peer_id,
            )
            if not payload:
                logger.warning("getprivatemessages returned nothing for peer %s", peer_id)
                continue
            result_obj = payload.get("ResultObject") or {}
            msgs = parse_private_messages(payload, result_obj.get("Recipient") or {})
            for m in msgs:
                # The dialog list carries the task this negotiation belongs to;
                # without it the card can only link to the sender's profile.
                task_id = dialog.get("TaskId")
                if task_id:
                    m["task_id"] = task_id
                task_title = dialog.get("TaskName")
                if task_title:
                    m["task_title"] = task_title
            collected.extend(msgs)

        if collected:
            logger.info("Fetched %d incoming chat message(s) from %d dialog(s)",
                        len(collected), len(unread))
        return collected
