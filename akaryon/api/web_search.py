import ipaddress
import re
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from akaryon.core.exceptions import AkaryonError

router = APIRouter(prefix="/search", tags=["web search"])


def _browser_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse browser-compatible IPv4 literals, including hex/octal/short forms.

    WHATWG URL parsing accepts forms such as ``127.1`` and ``0x7f000001``.
    Detect those before emitting links so API filtering agrees with browsers.
    Non-numeric DNS names return None; malformed numeric-looking hosts raise.
    """
    value = host[:-1] if host.endswith(".") else host
    parts = value.split(".")
    last = parts[-1] if parts else ""

    def number(component: str) -> int | None:
        if not component.isascii():
            return None
        radix, digits = 10, component
        if component[:2].casefold() == "0x":
            radix, digits = 16, component[2:]
            if not digits:
                return 0
        elif len(component) >= 2 and component.startswith("0"):
            radix, digits = 8, component[1:]
        if not digits:
            return None
        allowed = "0123456789abcdef"[:radix]
        if any(char.casefold() not in allowed for char in digits):
            return None
        return int(digits, radix)

    last_number = number(last)
    # WHATWG's "ends in a number" rule also treats a decimal-suffixed host as
    # an IPv4 candidate; reject it if its earlier components are not numeric.
    if last_number is None and not (last and last[-1].isascii() and last[-1].isdigit()):
        return None
    if not 1 <= len(parts) <= 4:
        raise ValueError("Invalid browser-style IPv4 address")
    values = [number(part) for part in parts]
    if any(part is None for part in values):
        raise ValueError("Invalid browser-style IPv4 address")
    numbers = [int(part) for part in values]
    if any(part > 255 for part in numbers[:-1]):
        raise ValueError("Invalid browser-style IPv4 address")
    if numbers[-1] >= 256 ** (5 - len(numbers)):
        raise ValueError("Invalid browser-style IPv4 address")
    packed = sum(part << (8 * (3 - index)) for index, part in enumerate(numbers[:-1]))
    packed += numbers[-1]
    return ipaddress.IPv4Address(packed)


def _valid_web_hostname(host: str) -> tuple[bool, str]:
    """Validate a DNS host with the same ASCII form a browser will navigate."""
    try:
        ascii_host = host.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return False, ""
    if not ascii_host or len(ascii_host) > 253:
        return False, ""
    labels = ascii_host.split(".")
    if len(labels) < 2:
        return False, ""
    for label in labels:
        if (not 1 <= len(label) <= 63 or label.startswith("-") or label.endswith("-") or
                re.fullmatch(r"[a-z0-9-]+", label) is None):
            return False, ""
    return True, ascii_host


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Search query cannot be blank")
        return value


def _safe_citations(items: object) -> list[dict[str, str | int]]:
    if not isinstance(items, (list, tuple)):
        return []
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or len(url) > 2048:
            continue
        try:
            parsed = urlsplit(url)
            _ = parsed.port
            host = parsed.hostname or ""
            if ":" in host:
                address = ipaddress.ip_address(host)
            else:
                valid_hostname, browser_host = _valid_web_hostname(host)
                if not valid_hostname:
                    continue
                address = _browser_ipv4(browser_host)
                # Keep numeric addresses readable and identical to the host a
                # browser will navigate to; drop obfuscated alternate forms.
                if address is not None and browser_host != str(address):
                    continue
        except ValueError:
            continue
        normalized_host = host.rstrip(".").casefold()
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or url in seen or
                normalized_host == "localhost" or normalized_host.endswith((".localhost", ".local", ".internal")) or
                (address is not None and not address.is_global)):
            continue
        seen.add(url)
        try:
            title = str(item.get("title") or url)[:500]
        except Exception:
            title = url
        citation = {"url": url, "title": title}
        start, end = item.get("start_index"), item.get("end_index")
        if (isinstance(start, int) and not isinstance(start, bool) and
                isinstance(end, int) and not isinstance(end, bool) and 0 <= start < end):
            citation.update(start_index=start, end_index=end)
        result.append(citation)
        if len(result) >= 20:
            break
    return result


@router.post("/web")
def search_web(body: WebSearchInput, request: Request) -> dict:
    settings = request.app.state.settings
    provider = request.app.state.model_router.providers.get("openai")
    if provider is None or not callable(getattr(provider, "search_web", None)):
        raise HTTPException(status_code=503,
                            detail="Web search requires the configured OpenAI provider and API key")
    estimate = settings.web_search_cost_reservation_usd
    task_limit = settings.max_estimated_cost_per_task_usd
    if task_limit is not None and estimate > task_limit:
        raise HTTPException(status_code=422,
                            detail="Web search's configured cost reservation exceeds the per-task cost cap")

    reservation_id = None
    if settings.max_estimated_cost_per_month_usd is not None:
        ledger = request.app.state.usage_ledger
        if ledger is None:
            raise HTTPException(status_code=503, detail="Monthly usage ledger is unavailable")
        operation_id = str(uuid4())
        try:
            reservation_id = ledger.reserve_fixed_cost(
                operation_id, operation_id, "openai", request.app.state.model_router.openai_search_model,
                estimate, settings.max_estimated_cost_per_month_usd)
        except ValueError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
    try:
        try:
            result = provider.search_web(body.query)
        except AkaryonError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        answer = result.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise HTTPException(status_code=502, detail="OpenAI web search returned an empty answer")
        citations = _safe_citations(result.get("citations", []))
        return {"answer": answer[:20000], "citations": citations,
                "model": str(result.get("model") or request.app.state.model_router.openai_search_model),
                "estimated_cost_usd": estimate,
                "disclosure": "Your search query is sent to OpenAI. Search results are temporary in this browser and are not saved by Akaryon."}
    finally:
        if reservation_id:
            request.app.state.usage_ledger.settle_estimate(reservation_id)
