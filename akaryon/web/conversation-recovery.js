(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AkaryonConversationRecovery = api;
})(typeof globalThis === 'undefined' ? this : globalThis, function () {
  'use strict';

  function clearMissingConversation(error, state, storage) {
    if (!error || error.status !== 404) return false;
    state.conversationId = '';
    storage.removeItem('akaryon.conversation');
    return true;
  }

  function clearMissingProject(projectId, projects, state, storage) {
    if (!projectId || projects.some(project => project.id === projectId)) return false;
    state.projectId = '';
    storage.removeItem('akaryon.project');
    return true;
  }

  return { clearMissingConversation, clearMissingProject };
});
