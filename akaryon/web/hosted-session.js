(() => {
  async function setup() {
    const response = await fetch('/auth/session', {cache: 'no-store'});
    if (response.status === 401) { location.assign('/signin'); return; }
    if (!response.ok) return;
    const session = await response.json();
    if (!session.hosted) return;
    document.documentElement.dataset.hosted = 'true';
    const label = document.querySelector('.top-user-copy small');
    if (label) label.textContent = 'Private workspace';
    const account = document.getElementById('hosted-account');
    account.classList.remove('hidden');
    document.getElementById('hosted-email').textContent = session.email;
    document.getElementById('hosted-logout').addEventListener('click', async () => {
      const result = await fetch('/auth/logout', {method: 'POST'});
      if (result.ok || result.status === 401) location.assign('/signin');
    });
  }
  setup().catch(() => {});
})();
