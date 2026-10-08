(() => {
  function createVoiceController(environment, callbacks = {}) {
    let recognition = null;
    let utterance = null;
    let speechButton = null;
    let stopButton = null;
    let microphonePermission = null;
    let microphonePermissionChanged = null;
    const speechPreferences = { voiceURI: '', rate: 1 };

    function getSpeechVoices() {
      return environment.speechSynthesis?.getVoices?.() || [];
    }

    async function checkMicrophonePermission() {
      const permissions = environment.navigator?.permissions;
      if (typeof permissions?.query !== 'function') return 'unknown';
      try {
        const result = await permissions.query({ name: 'microphone' });
        const permission = ['granted', 'denied', 'prompt'].includes(result?.state)
          ? result.state : 'unknown';
        if (microphonePermission && microphonePermissionChanged) {
          microphonePermission.removeEventListener?.('change', microphonePermissionChanged);
          if (microphonePermission.onchange === microphonePermissionChanged) microphonePermission.onchange = null;
        }
        microphonePermission = result;
        microphonePermissionChanged = () => {
          const current = ['granted', 'denied', 'prompt'].includes(result.state) ? result.state : 'unknown';
          callbacks.onMicrophonePermission?.(current);
        };
        if (typeof result.addEventListener === 'function') {
          result.addEventListener('change', microphonePermissionChanged);
        } else {
          result.onchange = microphonePermissionChanged;
        }
        callbacks.onMicrophonePermission?.(permission);
        return permission;
      } catch {
        return 'unknown';
      }
    }

    async function checkMicrophoneAvailability() {
      const mediaDevices = environment.navigator?.mediaDevices;
      if (typeof mediaDevices?.enumerateDevices !== 'function') {
        callbacks.onMicrophoneAvailability?.('unknown');
        return 'unknown';
      }
      try {
        const devices = await mediaDevices.enumerateDevices();
        const availability = Array.from(devices || []).some(device => device?.kind === 'audioinput')
          ? 'available' : 'not-detected';
        callbacks.onMicrophoneAvailability?.(availability);
        return availability;
      } catch {
        callbacks.onMicrophoneAvailability?.('unknown');
        return 'unknown';
      }
    }

    function dispose() {
      stopForNavigation();
      if (microphonePermission && microphonePermissionChanged) {
        microphonePermission.removeEventListener?.('change', microphonePermissionChanged);
        if (microphonePermission.onchange === microphonePermissionChanged) microphonePermission.onchange = null;
      }
      microphonePermission = microphonePermissionChanged = null;
    }

    function setSpeechPreferences(preferences = {}) {
      if (Object.hasOwn(preferences, 'voiceURI')) {
        speechPreferences.voiceURI = typeof preferences.voiceURI === 'string' ? preferences.voiceURI : '';
      }
      if (Object.hasOwn(preferences, 'rate')) {
        const rate = Number(preferences.rate);
        speechPreferences.rate = Number.isFinite(rate) ? Math.max(0.75, Math.min(1.25, rate)) : 1;
      }
      return { ...speechPreferences };
    }

    function stopDictation() {
      const active = recognition;
      recognition = null;
      if (active) { try { active.stop(); } catch { /* Recognition may already have stopped. */ } }
      callbacks.onDictationState?.(false);
    }

    function startDictation(input) {
      const Recognition = environment.SpeechRecognition || environment.webkitSpeechRecognition;
      if (!Recognition) { callbacks.onUnsupported?.(); return false; }
      if (recognition) { stopDictation(); return false; }
      const active = new Recognition();
      recognition = active;
      const original = input.value;
      input.readOnly = true;
      let finalTranscript = '';
      active.lang = environment.document?.documentElement?.lang || environment.navigator?.language || 'en-US';
      active.continuous = false;
      active.interimResults = true;
      callbacks.onDictationState?.(true);
      callbacks.onListening?.();
      active.onresult = event => {
        let interim = '';
        for (let index = event.resultIndex; index < event.results.length; index += 1) {
          const transcript = event.results[index][0].transcript;
          if (event.results[index].isFinal) finalTranscript += transcript;
          else interim += transcript;
        }
        const addition = `${finalTranscript}${interim ? `${finalTranscript ? ' ' : ''}${interim}` : ''}`;
        input.value = `${original}${original && addition ? (original.endsWith(' ') ? '' : ' ') : ''}${addition}`;
        input.focus();
      };
      active.onerror = event => {
        callbacks.onRecognitionError?.(event.error);
        if (recognition === active) stopDictation();
      };
      active.onend = () => {
        if (recognition !== active) return;
        recognition = null;
        input.readOnly = false;
        callbacks.onDictationState?.(false);
        if (finalTranscript.trim()) callbacks.onTranscript?.(input.value);
        else if (input.value === original) callbacks.onEmptyTranscript?.();
      };
      try { active.start(); }
      catch (error) {
        if (recognition === active) stopDictation();
        callbacks.onStartError?.(error);
        return false;
      }
      return true;
    }

    function stopSpeech() {
      const wasSpeaking = Boolean(utterance);
      if (environment.speechSynthesis) environment.speechSynthesis.cancel();
      if (speechButton) speechButton.textContent = 'Read aloud';
      if (stopButton) stopButton.hidden = true;
      utterance = speechButton = stopButton = null;
      if (wasSpeaking) callbacks.onSpeechState?.(false);
    }

    function stopForNavigation() {
      stopDictation();
      stopSpeech();
    }

    function toggleSpeech(value, button, haltButton) {
      if (!environment.speechSynthesis || !environment.SpeechSynthesisUtterance) {
        callbacks.onSpeechUnsupported?.();
        return;
      }
      if (utterance && speechButton === button) {
        if (environment.speechSynthesis.paused) {
          environment.speechSynthesis.resume(); button.textContent = 'Pause'; callbacks.onSpeechState?.(true);
        } else {
          environment.speechSynthesis.pause(); button.textContent = 'Resume'; callbacks.onSpeechState?.(false);
        }
        return;
      }
      stopSpeech();
      environment.speechSynthesis.cancel();
      const active = new environment.SpeechSynthesisUtterance(value);
      const selectedVoice = getSpeechVoices().find(voice => voice.voiceURI === speechPreferences.voiceURI);
      if (selectedVoice) {
        active.voice = selectedVoice;
        if (selectedVoice.lang) active.lang = selectedVoice.lang;
      }
      active.rate = speechPreferences.rate;
      utterance = active; speechButton = button; stopButton = haltButton;
      button.textContent = 'Pause'; haltButton.hidden = false;
      active.onstart = () => { if (utterance === active) callbacks.onSpeechState?.(true); };
      active.onend = () => { if (utterance === active) stopSpeech(); };
      active.onerror = event => {
        if (utterance === active) stopSpeech();
        if (event.error !== 'canceled' && event.error !== 'interrupted') callbacks.onSpeechError?.(event.error);
      };
      environment.speechSynthesis.speak(active);
    }

    return { startDictation, checkMicrophonePermission, checkMicrophoneAvailability, stopDictation, toggleSpeech, stopSpeech, stopForNavigation, dispose,
      getSpeechVoices, setSpeechPreferences,
      get dictationActive() { return recognition !== null; } };
  }

  if (typeof module === 'object' && module.exports) module.exports = { createVoiceController };
  else window.AkaryonVoice = { createVoiceController };
})();
