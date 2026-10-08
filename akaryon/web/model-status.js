((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AkaryonModelStatus = api;
})(typeof globalThis === 'undefined' ? this : globalThis, () => {
  function providerStatus(provider) {
    if (!provider) return 'Not configured';
    if (provider.checking || provider.status === 'checking') return 'Checking readiness';
    if (provider.status === 'online') return 'Ready';
    if (provider.status === 'offline') return 'Server offline';
    if (provider.status === 'model_missing') return 'Model not installed';
    if (provider.status === 'configured') return 'Configured · checked on use';
    if (provider.status === 'local') return 'Local deterministic provider';
    return String(provider.status || 'Unavailable').replaceAll('_', ' ');
  }

  function forHealth(health = {}) {
    const providers = Array.isArray(health.available_providers) ? health.available_providers : [];
    const byId = id => providers.find(provider => provider.id === id);
    const visionAvailable = Boolean(byId('openai')?.capabilities?.includes('image_input'));
    const providerCard = provider => {
      const name = provider.id === 'ollama' ? 'Ollama' :
        provider.id === 'openai' ? 'OpenAI' :
          provider.id === 'anthropic' ? 'Anthropic' : provider.id;
      const isLocal = provider.id === 'ollama' || provider.id === 'mock';
      return {
        name,
        detail: `${provider.model} · ${isLocal ? 'Local provider' : 'Cloud provider'}`,
        description: providerStatus(provider),
        status: providerStatus(provider),
        active: false,
        icon: provider.id === 'ollama' ? '⌂' : provider.id === 'mock' ? '◇' : '◉',
      };
    };
    const featureCard = (name, detail, description, available, icon) => ({
      name, detail, description, status: available ? 'Configured' : 'Not configured',
      active: false, icon,
    });
    const cards = [{
      name: 'Active runtime',
      detail: `${health.provider || 'Unknown provider'} · ${health.model || 'No model reported'}`,
      description: 'The provider currently selected by the Akaryon runtime.',
      status: 'Active', active: true, icon: '✳',
    }, ...providers.filter(provider => provider.id !== health.provider).map(providerCard),
    featureCard('Vision input', 'OpenAI image analysis',
      visionAvailable ? 'Supported image attachments are sent to OpenAI for analysis.' : 'Configure OpenAI to analyze image attachments.',
      visionAvailable, '◎'),
    featureCard('Image Studio', health.image_generation_model || 'OpenAI Images API',
      health.image_generation_available ? 'Image generation is configured; prompts go to OpenAI and results remain temporary in this browser.' : 'Configure OpenAI to enable image generation.',
      Boolean(health.image_generation_available), '▧'),
    featureCard('Web Search', health.openai_search_model || 'OpenAI search',
      health.web_search_available ? 'Cited web search is available as a separate user-started workflow.' : 'Configure OpenAI to enable web search.',
      Boolean(health.web_search_available), '⌕')];

    const stack = [{
      icon: '✳', name: health.model || 'Active runtime',
      detail: `${health.provider || 'Provider'} · active`, badge: 'Active', active: true,
    }, ...providers.filter(provider => provider.id !== health.provider).map(provider => ({
      icon: provider.id === 'ollama' ? '⌂' : provider.id === 'anthropic' ? '◈' : '◉',
      name: `${provider.id} · ${provider.model}`,
      detail: provider.id === 'ollama' ? 'Local inference' : provider.id === 'mock' ? 'Offline test provider' : 'Cloud provider',
      badge: providerStatus(provider), active: false,
    })), {
      icon: '◎', name: 'Vision input', detail: 'OpenAI image analysis',
      badge: visionAvailable ? 'Configured' : 'Not configured', active: false,
    }, {
      icon: '▧', name: `Image Studio · ${health.image_generation_model || 'OpenAI Images API'}`,
      detail: 'Image generation', badge: health.image_generation_available ? 'Configured' : 'Not configured', active: false,
    }, {
      icon: '⌕', name: `Web Search · ${health.openai_search_model || 'OpenAI search'}`,
      detail: 'Cited web search', badge: health.web_search_available ? 'Configured' : 'Not configured', active: false,
    }];
    return { cards, stack };
  }

  return { forHealth, providerStatus };
});
