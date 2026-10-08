"""Loopback-only approval dashboard that keeps the approval token server-side."""

from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from akaryon.api import approvals as approvals_api

router = APIRouter(prefix="/approvals/ui", tags=["approval UI"])

_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Akaryon approvals</title>
  <style>
    :root { color-scheme: dark; font: 16px/1.5 system-ui, sans-serif; background: #11151b; color: #e8edf4; }
    body { margin: 0 auto; max-width: 900px; padding: 2rem 1.25rem 4rem; }
    h1 { margin-bottom: .25rem; }
    .muted { color: #aab5c3; }
    #status { min-height: 1.5rem; margin: 1rem 0; }
    .card { background: #1b222c; border: 1px solid #354150; border-radius: 12px; margin: 1rem 0; padding: 1.1rem; }
    .facts { display: grid; grid-template-columns: minmax(8rem, 10rem) 1fr; gap: .35rem 1rem; }
    .facts dt { color: #aab5c3; }
    .facts dd { margin: 0; overflow-wrap: anywhere; }
    pre { background: #11151b; border-radius: 8px; overflow: auto; padding: .8rem; white-space: pre-wrap; overflow-wrap: anywhere; }
    button { border: 0; border-radius: 7px; cursor: pointer; font: inherit; font-weight: 650; margin: .7rem .55rem 0 0; padding: .6rem 1rem; }
    button:disabled { cursor: wait; opacity: .6; }
    .approve { background: #69d39a; color: #0d2116; }
    .deny { background: #353f4c; color: #f3f6fa; }
    .error { color: #ff9999; }
    .empty { border: 1px dashed #526070; border-radius: 10px; padding: 1rem; }
    .toolbar { display:flex; flex-wrap:wrap; gap:.5rem 1.2rem; padding:.8rem 1rem; background:#1b222c; border-radius:10px; }
    a { color:#9bc7ff; }
    @media (max-width: 540px) { .facts { grid-template-columns: 1fr; gap: 0; } .facts dd { margin-bottom: .6rem; } }
  </style>
</head>
<body>
  <h1>Akaryon approvals</h1>
  <p class="muted">Review each requested action and its arguments. Approving runs that action once. The approval credential stays in Akaryon.</p>
  <section class="toolbar" id="health" aria-label="Akaryon status">Loading local status…</section>
  <h2>Pending approvals</h2>
  <p id="status" role="status" aria-live="polite">Loading approvals…</p>
  <main id="approvals"></main>
  <h2>Recent decisions</h2>
  <main id="history"></main>
  <script>
    const list = document.querySelector('#approvals');
    const history = document.querySelector('#history');
    const health = document.querySelector('#health');
    const status = document.querySelector('#status');
    const esc = value => String(value ?? '');
    function field(parent, label, value) {
      const dt = document.createElement('dt'); dt.textContent = label;
      const dd = document.createElement('dd'); dd.textContent = esc(value);
      parent.append(dt, dd);
    }
    function makeLink(label, href, text) {
      const a = document.createElement('a'); a.href = href; a.textContent = `${label}: ${text}`; return a;
    }
    async function refreshHealth() {
      try {
        const response = await fetch('/health', { headers: { 'Accept': 'application/json' } });
        const data = await response.json();
        if (!response.ok) throw new Error('Status unavailable');
        health.replaceChildren();
        for (const value of [`Provider: ${data.provider}`, `Model: ${data.model}`,
          `Database: ${data.database_enabled ? 'enabled' : 'in-memory'}`,
          `Approvals: ${data.approval_enabled ? 'enabled' : 'disabled'}`]) {
          const span = document.createElement('span'); span.textContent = value; health.append(span);
        }
      } catch { health.textContent = 'Local status unavailable.'; }
    }
    function renderHistory(items) {
      history.replaceChildren();
      if (!items.length) { const empty=document.createElement('p'); empty.className='empty'; empty.textContent='No decisions recorded yet.'; history.append(empty); return; }
      for (const item of items) {
        const card=document.createElement('section'); card.className='card';
        const title=document.createElement('h3'); title.textContent=`${item.tool} · ${item.status}`;
        const facts=document.createElement('dl'); facts.className='facts';
        field(facts, 'Approval ID', item.id); field(facts, 'Decision time', item.decided_at || 'Unknown');
        const links=document.createElement('p'); links.append(makeLink('Task', `/tasks/${encodeURIComponent(item.task_id)}`, item.task_id));
        if (item.session_id) links.append(document.createTextNode(' · '), makeLink('Manager session', `/agents/manager/sessions/${encodeURIComponent(item.session_id)}`, item.session_id));
        card.append(title, facts, links); history.append(card);
      }
    }
    async function decide(item, approved, buttons) {
      const verb = approved ? 'Approve' : 'Deny';
      if (!window.confirm(`${verb} this ${item.tool} action? Review its arguments on the page first.`)) return;
      buttons.forEach(button => button.disabled = true);
      try {
        const response = await fetch(`/approvals/ui/api/${encodeURIComponent(item.id)}/decision`, {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
          body: JSON.stringify({ approved })
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || `Request failed (${response.status})`);
        status.textContent = approved ? 'Action approved and completed.' : 'Action denied.';
        await refresh();
      } catch (error) {
        status.textContent = error.message;
        status.className = 'error';
        buttons.forEach(button => button.disabled = false);
      }
    }
    async function refresh() {
      try {
        const response = await fetch('/approvals/ui/api/pending', { headers: { 'Accept': 'application/json' } });
        const items = await response.json();
        if (!response.ok) throw new Error(items.detail || `Request failed (${response.status})`);
        list.replaceChildren(); status.className = '';
        if (!items.length) {
          const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = 'No pending approvals.';
          list.append(empty); status.textContent = 'Updated just now.';
        } else {
          status.textContent = `${items.length} pending action${items.length === 1 ? '' : 's'}.`;
        }
        for (const item of items) {
          const card = document.createElement('section'); card.className = 'card';
          const title = document.createElement('h2'); title.textContent = `${item.tool} · ${item.capability}`;
          const facts = document.createElement('dl'); facts.className = 'facts';
          field(facts, 'Approval ID', item.id); field(facts, 'Task ID', item.task_id);
          field(facts, 'Agent', item.agent_id); field(facts, 'Requested action', item.tool);
          if (item.scope) field(facts, 'Workspace scope', item.scope);
          if (item.expires_at) field(facts, 'Expires', item.expires_at);
          if (item.provider === 'codex_cli') {
            const operation = item.arguments?.operation || 'filesystem action';
            field(facts, 'Impact', `${operation} ${item.arguments?.path || ''}`.trim());
          } else if (item.tool === 'open_browser') {
            field(facts, 'Impact', `Open ${item.arguments?.url || 'the requested URL'} in the default browser`);
          } else if (item.tool === 'windows_app') {
            const operation = item.arguments?.operation;
            const impact = operation === 'open'
              ? `Launch allowlisted Windows app ${item.arguments?.application || ''}`.trim()
              : `Send a graceful close request to the Akaryon-launched app ${item.arguments?.launch_id || ''}`.trim();
            field(facts, 'Impact', `${impact}. This never force-terminates the app.`);
          }
          const links=document.createElement('p'); links.append(makeLink('Task', `/tasks/${encodeURIComponent(item.task_id)}`, item.task_id));
          if (item.session_id) links.append(document.createTextNode(' · '), makeLink('Manager session', `/agents/manager/sessions/${encodeURIComponent(item.session_id)}`, item.session_id));
          const argsLabel = document.createElement('h3'); argsLabel.textContent = 'Arguments';
          const args = document.createElement('pre'); args.textContent = JSON.stringify(item.arguments, null, 2);
          const approve = document.createElement('button'); approve.className = 'approve'; approve.textContent = 'Approve';
          const deny = document.createElement('button'); deny.className = 'deny'; deny.textContent = 'Deny';
          const buttons = [approve, deny];
          approve.addEventListener('click', () => decide(item, true, buttons));
          deny.addEventListener('click', () => decide(item, false, buttons));
          card.append(title, facts, links, argsLabel, args, ...buttons); list.append(card);
        }
        const historyResponse = await fetch('/approvals/ui/api/history', { headers: { 'Accept': 'application/json' } });
        const historyItems = await historyResponse.json();
        if (!historyResponse.ok) throw new Error(historyItems.detail || `Request failed (${historyResponse.status})`);
        renderHistory(historyItems);
      } catch (error) {
        status.textContent = error.message; status.className = 'error';
      }
    }
    refresh(); refreshHealth(); setInterval(refresh, 5000); setInterval(refreshHealth, 30000);
  </script>
</body>
</html>"""


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _require_loopback(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if not _is_loopback(client_host) or not _is_loopback(request.url.hostname):
        raise HTTPException(status_code=403, detail="Approval dashboard is available on this computer only")


def _require_same_origin(request: Request, *, require_origin: bool) -> None:
    _require_loopback(request)
    if request.headers.get("sec-fetch-site") != "same-origin":
        raise HTTPException(status_code=403, detail="Approval request must come from the local dashboard")
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if (require_origin and not origin) or (origin and urlsplit(origin).scheme + "://" + urlsplit(origin).netloc != expected):
        raise HTTPException(status_code=403, detail="Approval request origin does not match this server")


@router.get("", response_class=HTMLResponse)
def approval_dashboard(request: Request) -> HTMLResponse:
    _require_loopback(request)
    response = HTMLResponse(_PAGE)
    response.headers.update({
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    })
    return response


@router.get("/api/pending")
def dashboard_pending(request: Request) -> list[dict]:
    _require_same_origin(request, require_origin=False)
    token = request.app.state.settings.approval_token
    authorization = f"Bearer {token}" if token else None
    items = approvals_api.list_pending_approvals(request, authorization)
    for item in items:
        session = request.app.state.agent_sessions.get_for_task(item["task_id"])
        item["session_id"] = session.id if session else None
    return items


@router.get("/api/history")
def dashboard_history(request: Request) -> list[dict]:
    _require_same_origin(request, require_origin=False)
    token = request.app.state.settings.approval_token
    authorization = f"Bearer {token}" if token else None
    items = approvals_api.list_recent_approvals(request, authorization)
    for item in items:
        session = request.app.state.agent_sessions.get_for_task(item["task_id"])
        item["session_id"] = session.id if session else None
    return items


@router.post("/api/{approval_id}/decision")
def dashboard_decision(approval_id: str, body: approvals_api.ApprovalDecision,
                       request: Request) -> dict:
    _require_same_origin(request, require_origin=True)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Approval decision must be JSON")
    token = request.app.state.settings.approval_token
    authorization = f"Bearer {token}" if token else None
    approvals_api._authorize(request, authorization)
    return approvals_api.apply_approval_decision(approval_id, body, request)
