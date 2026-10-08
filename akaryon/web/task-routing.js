(function (root) {
  function taskRouteOptions(routes) {
    if (!Array.isArray(routes)) return [];
    return routes.filter(route => route &&
      typeof route.task_type === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(route.task_type) &&
      typeof route.provider === 'string')
      .map(route => {
        const target = [route.provider, route.model].filter(Boolean).join(' / ');
        const available = route.available !== false;
        return {
          value: route.task_type,
          label: `${route.task_type} → ${target}${available ? '' : ' · unavailable'}`,
          title: available
            ? `Use the configured ${route.provider}${route.model ? ` / ${route.model}` : ''} route for ${route.task_type}.`
            : `The configured ${route.provider} route for ${route.task_type} is unavailable; this request will fail without switching providers.`,
        };
      });
  }

  function taskTypePayload(agentBackend, taskType) {
    return agentBackend === 'native' && typeof taskType === 'string' && taskType
      ? { task_type: taskType }
      : {};
  }

  root.AkaryonTaskRouting = { taskRouteOptions, taskTypePayload };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.AkaryonTaskRouting;
})(globalThis);
