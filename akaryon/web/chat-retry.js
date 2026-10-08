(function (root) {
  function restoreFileSelection(input, files, createTransfer = () => new DataTransfer()) {
    const transfer = createTransfer();
    files.forEach(file => transfer.items.add(file));
    input.files = transfer.files;
    return [...transfer.files];
  }

  function bindChatRetry(button, input, form, message, restoreSettings = () => {}) {
    button.type = 'button';
    button.addEventListener('click', () => {
      restoreSettings();
      input.value = message;
      input.focus();
      form.requestSubmit();
    });
  }

  root.AkaryonChatRetry = { bindChatRetry, restoreFileSelection };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.AkaryonChatRetry;
})(globalThis);
