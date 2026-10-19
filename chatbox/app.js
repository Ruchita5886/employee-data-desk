const form = document.querySelector('#chat-form');
const input = document.querySelector('#question');
const sendButton = document.querySelector('#send-button');
const messages = document.querySelector('#messages');
const welcome = document.querySelector('#welcome');
const chatHistoryPanel = document.querySelector('#chat-history');
const chatHistoryList = document.querySelector('#chat-history-list');
const chatHistoryEmpty = document.querySelector('#chat-history-empty');
const chatHistoryStorageWarning = document.querySelector('#chat-history-storage-warning');
const recordButton = document.querySelector('#record-button');
const voiceStatus = document.querySelector('#voice-status');
const questionHistory = [];
const CONVERSATION_STORAGE_KEY = 'query-desk-conversations-v1';
const CONVERSATION_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
let savedConversations = loadSavedConversations();
let activeConversation = createConversation();
const speechOutputSupported = 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window;
const SpeechRecognitionApi = window.SpeechRecognition || window.webkitSpeechRecognition;
const browserSpeechRecognitionSupported = Boolean(SpeechRecognitionApi);
const audioRecordingSupported = typeof MediaRecorder !== 'undefined' && Boolean(navigator.mediaDevices?.getUserMedia);
let activeAudioButton = null;
let transcriptionReady = false;
let activeRecorder = null;
let activeSpeechRecognition = null;
let recordingStream = null;
let recordingChunks = [];
let recordingPrefix = '';
let recordingTimer = null;
let recordingStartedAt = 0;
let transcriptionPending = false;
let browserRecognitionTimer = null;
let voicePressActive = false;
let voiceReleasePending = false;
let voiceTranscriptReady = false;
const suggestions = [
  ['↗', 'Active Support employees', 'Show employees in Support with Active status'],
  ['＄', 'Top salaries', 'Show employees earning more than 90000, highest to lowest'],
  ['▦', 'Headcount by city', 'Count employees by city'],
  ['∑', 'Department payroll', 'Total salary by department']
];

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
}

function setTheme(theme) {
  const selectedTheme = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = selectedTheme;
  document.querySelector('meta[name="theme-color"]').content = selectedTheme === 'dark' ? '#151c1a' : '#f5f7f5';
  document.querySelectorAll('[data-theme-choice]').forEach((button) => {
    const selected = button.dataset.themeChoice === selectedTheme;
    button.setAttribute('aria-pressed', String(selected));
  });
  try { localStorage.setItem('query-desk-theme', selectedTheme); } catch {}
}

document.querySelectorAll('[data-theme-choice]').forEach((button) => {
  button.addEventListener('click', () => setTheme(button.dataset.themeChoice));
});
try { setTheme(localStorage.getItem('query-desk-theme') || 'light'); } catch { setTheme('light'); }

function setAudioButtonLabel(button, text) {
  const label = button.querySelector('.audio-label');
  if (label) label.textContent = text;
  button.setAttribute('aria-label', `${text} response audio`);
}

function stopReplyAudio() {
  if (speechOutputSupported) window.speechSynthesis.cancel();
  if (activeAudioButton) setAudioButtonLabel(activeAudioButton, 'Listen');
  activeAudioButton = null;
}

function speakResponse(text, button) {
  if (!speechOutputSupported || !text) return;
  if (activeAudioButton === button) {
    stopReplyAudio();
    return;
  }
  stopReplyAudio();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = navigator.language || 'en-US';
  activeAudioButton = button;
  setAudioButtonLabel(button, 'Stop');
  utterance.onend = () => {
    if (activeAudioButton === button) {
      setAudioButtonLabel(button, 'Listen');
      activeAudioButton = null;
    }
  };
  utterance.onerror = utterance.onend;
  window.speechSynthesis.speak(utterance);
}

function updateInputHeight() {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

function updateTranscriptionAvailability(ready) {
  transcriptionReady = Boolean(ready);
  if (activeRecorder?.state === 'recording' || activeSpeechRecognition || transcriptionPending) return;
  if (browserSpeechRecognitionSupported) {
    recordButton.disabled = false;
    recordButton.title = 'Hold to speak, then release to send';
    voiceStatus.textContent = 'Browser speech input available';
  } else if (!audioRecordingSupported) {
    recordButton.disabled = true;
    recordButton.title = 'Microphone recording is not supported in this browser';
    voiceStatus.textContent = 'Microphone unavailable';
  } else if (!transcriptionReady) {
    recordButton.disabled = true;
    recordButton.title = 'Deploy Azure OpenAI Whisper and set AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT in .env';
    voiceStatus.textContent = 'Voice setup required';
  } else {
    recordButton.disabled = false;
    recordButton.title = 'Start recording a question';
    voiceStatus.textContent = '';
  }
}

function setRecordingState(recording) {
  recordButton.classList.toggle('recording', recording);
  recordButton.setAttribute('aria-pressed', String(recording));
  recordButton.setAttribute('aria-label', recording ? 'Release to stop and send voice input' : 'Hold to ask by voice');
  recordButton.title = recording ? 'Release to stop and send your question' : 'Hold to speak, then release to send';
}

function stopRecordingStream() {
  window.clearInterval(recordingTimer);
  recordingTimer = null;
  recordingStream?.getTracks().forEach((track) => track.stop());
  recordingStream = null;
}

async function transcribeRecording(audioBlob) {
  transcriptionPending = true;
  let transcriptAvailable = false;
  recordButton.disabled = true;
  voiceStatus.textContent = 'Transcribing recording...';
  try {
    const response = await fetch('/api/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': audioBlob.type || 'audio/webm' },
      body: audioBlob
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Transcription failed. Try recording again.');
    const transcript = String(payload.text || '').trim();
    if (!transcript) throw new Error('No speech detected. Try recording again in a quieter place.');
    if (transcript.length > 4000) throw new Error('That transcript is over 4,000 characters. Record a shorter message.');
    input.value = [recordingPrefix, transcript].filter(Boolean).join(' ');
    updateInputHeight();
    input.focus();
    transcriptAvailable = true;
    voiceStatus.textContent = 'Transcript ready. Sending...';
  } catch (error) {
    voiceStatus.textContent = error.message || 'Could not transcribe this recording.';
  } finally {
    transcriptionPending = false;
    recordButton.disabled = !browserSpeechRecognitionSupported && (!transcriptionReady || !audioRecordingSupported);
    if (voiceReleasePending) {
      voiceReleasePending = false;
      if (transcriptAvailable) void submitQuestion(input.value);
    }
  }
}

async function startAudioRecording() {
  if (!voicePressActive || !transcriptionReady || !audioRecordingSupported) return;
  try {
    recordingStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    });
    if (!voicePressActive) {
      stopRecordingStream();
      return;
    }
    const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
      .find((candidate) => MediaRecorder.isTypeSupported?.(candidate));
    const recorder = new MediaRecorder(recordingStream, mimeType ? { mimeType } : undefined);
    activeRecorder = recorder;
    recordingChunks = [];
    recordingPrefix = input.value.trim();
    recorder.addEventListener('dataavailable', (event) => {
      if (event.data.size) recordingChunks.push(event.data);
    });
    recorder.addEventListener('error', () => {
      stopRecordingStream();
      activeRecorder = null;
      voicePressActive = false;
      voiceReleasePending = false;
      setRecordingState(false);
      voiceStatus.textContent = 'Recording failed. Check microphone access and try again.';
    }, { once: true });
    recorder.addEventListener('stop', () => {
      const audioBlob = new Blob(recordingChunks, { type: recorder.mimeType || mimeType || 'audio/webm' });
      recordingChunks = [];
      activeRecorder = null;
      stopRecordingStream();
      setRecordingState(false);
      void transcribeRecording(audioBlob);
    }, { once: true });
    recorder.start(250);
    recordingStartedAt = Date.now();
    setRecordingState(true);
    const updateRecordingTimer = () => {
      const elapsed = Math.floor((Date.now() - recordingStartedAt) / 1000);
      const minutes = String(Math.floor(elapsed / 60)).padStart(2, '0');
      const seconds = String(elapsed % 60).padStart(2, '0');
      voiceStatus.textContent = `Recording ${minutes}:${seconds}. Stop when finished.`;
      if (elapsed >= 60 && activeRecorder?.state === 'recording') {
        voicePressActive = false;
        voiceReleasePending = true;
        voiceStatus.textContent = '60-second limit reached. Transcribing...';
        activeRecorder.stop();
      }
    };
    updateRecordingTimer();
    recordingTimer = window.setInterval(updateRecordingTimer, 500);
  } catch (error) {
    voicePressActive = false;
    voiceReleasePending = false;
    stopRecordingStream();
    activeRecorder = null;
    setRecordingState(false);
    voiceStatus.textContent = error.name === 'NotAllowedError'
      ? 'Allow microphone access, then try again.'
      : 'Could not start microphone recording.';
  }
}

function startBrowserSpeechRecognition() {
  recordingPrefix = input.value.trim();
  let transcript = '';
  let recognitionFailure = '';
  let useAudioFallback = false;
  try {
    const recognition = new SpeechRecognitionApi();
    activeSpeechRecognition = recognition;
    recognition.lang = navigator.language || 'en-US';
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.onstart = () => {
      setRecordingState(true);
      voiceStatus.textContent = 'Listening... release to send.';
      browserRecognitionTimer = window.setTimeout(() => {
        if (!activeSpeechRecognition) return;
        voicePressActive = false;
        voiceReleasePending = true;
        voiceStatus.textContent = '60-second limit reached. Finishing...';
        activeSpeechRecognition.stop();
      }, 60_000);
    };
    recognition.onresult = (event) => {
      let interim = '';
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        const words = String(result[0]?.transcript || '').trim();
        if (result.isFinal) transcript += `${words} `;
        else interim += `${words} `;
      }
      voiceStatus.textContent = interim.trim() ? `Listening: ${interim.trim()}` : 'Listening... release to send.';
    };
    recognition.onerror = (event) => {
      if (event.error === 'network' && transcriptionReady && audioRecordingSupported) {
        useAudioFallback = true;
        voiceStatus.textContent = 'Browser speech service unavailable. Switching to server transcription...';
        return;
      }
      const errors = {
        'not-allowed': 'Allow microphone access, then try again.',
        'service-not-allowed': 'This browser is blocking its speech recognition service.',
        'audio-capture': 'No microphone is available. Check your system audio settings.',
        network: 'Browser speech service is unreachable. Check your connection or configure Azure Whisper transcription.',
        'no-speech': 'No speech detected. Try again.',
        'language-not-supported': 'This browser does not support the selected speech language.'
      };
      recognitionFailure = errors[event.error] || `Speech recognition failed (${event.error || 'unknown error'}).`;
    };
    recognition.onend = () => {
      window.clearTimeout(browserRecognitionTimer);
      browserRecognitionTimer = null;
      activeSpeechRecognition = null;
      setRecordingState(false);
      updateTranscriptionAvailability(transcriptionReady);
      if (useAudioFallback) {
        if (voicePressActive) {
          voiceStatus.textContent = 'Recording for server transcription... release to send.';
          void startAudioRecording();
        } else {
          voiceReleasePending = false;
          voiceStatus.textContent = 'Browser speech service failed. Hold again to retry with server transcription.';
        }
        return;
      }
      const words = transcript.trim();
      if (words) {
        const availableLength = Math.max(0, 4000 - recordingPrefix.length - (recordingPrefix ? 1 : 0));
        const clippedTranscript = words.slice(0, availableLength);
        input.value = [recordingPrefix, clippedTranscript].filter(Boolean).join(' ');
        updateInputHeight();
        voiceTranscriptReady = Boolean(clippedTranscript);
        if (voiceReleasePending && voiceTranscriptReady) {
          voiceReleasePending = false;
          voiceTranscriptReady = false;
          voiceStatus.textContent = 'Transcript ready. Sending...';
          void submitQuestion(input.value);
        } else {
          voiceStatus.textContent = clippedTranscript.length < words.length
            ? 'Transcript shortened. Release to send.'
            : 'Speech captured. Release to send.';
        }
      } else {
        voiceReleasePending = false;
        voiceStatus.textContent = recognitionFailure || 'No speech detected. Try again.';
      }
    };
    recognition.start();
  } catch {
    activeSpeechRecognition = null;
    voicePressActive = false;
    voiceReleasePending = false;
    setRecordingState(false);
    updateTranscriptionAvailability(transcriptionReady);
    voiceStatus.textContent = 'Could not start speech recognition. Check microphone access and try again.';
  }
}

function startVoiceInput() {
  if (voicePressActive || sendButton.disabled || recordButton.disabled) return;
  voicePressActive = true;
  voiceReleasePending = false;
  voiceTranscriptReady = false;
  if (browserSpeechRecognitionSupported) startBrowserSpeechRecognition();
  else void startAudioRecording();
}

function releaseVoiceInput() {
  if (!voicePressActive) return;
  voicePressActive = false;
  voiceReleasePending = true;
  if (activeSpeechRecognition) {
    voiceStatus.textContent = 'Finishing voice input...';
    try { activeSpeechRecognition.stop(); } catch { activeSpeechRecognition.abort(); }
  } else if (activeRecorder?.state === 'recording') {
    voiceStatus.textContent = 'Transcribing recording...';
    activeRecorder.stop();
  } else if (voiceTranscriptReady) {
    voiceTranscriptReady = false;
    voiceReleasePending = false;
    voiceStatus.textContent = 'Transcript ready. Sending...';
    void submitQuestion(input.value);
  } else if (!recordingStream) {
    voiceReleasePending = false;
  }
}

recordButton.addEventListener('pointerdown', (event) => {
  if (event.button !== 0) return;
  event.preventDefault();
  recordButton.setPointerCapture(event.pointerId);
  startVoiceInput();
});
recordButton.addEventListener('pointerup', releaseVoiceInput);
recordButton.addEventListener('pointercancel', releaseVoiceInput);
recordButton.addEventListener('keydown', (event) => {
  if ((event.code === 'Space' || event.code === 'Enter') && !event.repeat) {
    event.preventDefault();
    startVoiceInput();
  }
});
recordButton.addEventListener('keyup', (event) => {
  if (event.code === 'Space' || event.code === 'Enter') {
    event.preventDefault();
    releaseVoiceInput();
  }
});
updateTranscriptionAvailability(false);

function createConversation() {
  const now = Date.now();
  return {
    id: globalThis.crypto?.randomUUID?.() || `${now}-${Math.random().toString(36).slice(2)}`,
    title: 'New conversation',
    createdAt: now,
    updatedAt: now,
    messages: []
  };
}

function loadSavedConversations() {
  try {
    const stored = JSON.parse(localStorage.getItem(CONVERSATION_STORAGE_KEY) || '[]');
    if (!Array.isArray(stored)) return [];
    const cutoff = Date.now() - CONVERSATION_RETENTION_MS;
    const conversations = stored
      .filter((conversation) => conversation
        && typeof conversation.id === 'string'
        && typeof conversation.updatedAt === 'number'
        && conversation.updatedAt >= cutoff
        && Array.isArray(conversation.messages))
      .sort((first, second) => second.updatedAt - first.updatedAt);
    try {
      localStorage.setItem(CONVERSATION_STORAGE_KEY, JSON.stringify(conversations));
      chatHistoryStorageWarning.hidden = true;
    } catch {
      chatHistoryStorageWarning.hidden = false;
    }
    return conversations;
  } catch {
    chatHistoryStorageWarning.hidden = false;
    return [];
  }
}

function saveConversationHistory() {
  const cutoff = Date.now() - CONVERSATION_RETENTION_MS;
  savedConversations = savedConversations.filter((conversation) =>
    conversation.id !== activeConversation.id && conversation.updatedAt >= cutoff
  );
  if (activeConversation.messages.length) savedConversations.unshift(activeConversation);
  savedConversations.sort((first, second) => second.updatedAt - first.updatedAt);
  try {
    localStorage.setItem(CONVERSATION_STORAGE_KEY, JSON.stringify(savedConversations));
    chatHistoryStorageWarning.hidden = true;
  } catch {
    chatHistoryStorageWarning.hidden = false;
  }
  renderConversationHistory();
}

function appendConversationMessage(message) {
  activeConversation.messages.push(message);
  activeConversation.updatedAt = Date.now();
  if (message.role === 'user' && activeConversation.title === 'New conversation') {
    const title = message.text.replace(/\s+/g, ' ').trim();
    activeConversation.title = title.length > 44 ? `${title.slice(0, 41)}...` : title;
  }
  saveConversationHistory();
}

function renderConversationHistory() {
  chatHistoryList.replaceChildren();
  savedConversations.forEach((conversation) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'chat-history-item';
    button.textContent = conversation.title || 'Previous conversation';
    button.title = new Date(conversation.updatedAt).toLocaleString();
    button.setAttribute('aria-current', String(conversation.id === activeConversation.id));
    button.disabled = sendButton.disabled;
    button.addEventListener('click', () => openSavedConversation(conversation.id));
    chatHistoryList.append(button);
  });
  chatHistoryEmpty.hidden = savedConversations.length > 0;
}

function openSavedConversation(id) {
  if (sendButton.disabled) return;
  const conversation = savedConversations.find((entry) => entry.id === id);
  if (!conversation) return;
  activeConversation = conversation;
  questionHistory.splice(0, questionHistory.length, ...conversation.messages
    .filter((entry) => entry.role === 'user' && !entry.reportRequest)
    .map((entry) => entry.text));
  releasePdfPreviews(messages);
  messages.replaceChildren();
  conversation.messages.forEach((entry) => {
    if (entry.role === 'user') {
      const userMessage = addUserMessage(entry.text);
      if (entry.reportRequest) userMessage.dataset.reportRequest = 'true';
    } else if (entry.kind === 'result' && entry.result) {
      const response = document.createElement('div');
      response.className = 'message assistant-message result-message';
      renderResults(response, entry.result, entry.question || '');
      messages.append(response);
    } else if (entry.kind === 'error') {
      const response = document.createElement('div');
      response.className = 'message assistant-message';
      renderError(response, entry.text);
      messages.append(response);
    } else if (entry.kind === 'report') {
      const response = document.createElement('div');
      response.className = 'message assistant-message result-message';
      const answer = document.createElement('div');
      answer.className = 'answer-copy';
      answer.textContent = entry.text || 'Your PDF report is ready to preview.';
      response.append(answer);
      if (entry.pdf) {
        renderReportPreview(response, base64ToPdf(entry.pdf), entry.filename || 'business_report.pdf', entry.question || '');
      }
      messages.append(response);
    }
  });
  welcome.hidden = conversation.messages.length > 0;
  input.value = '';
  input.style.height = 'auto';
  renderConversationHistory();
  if (window.matchMedia('(max-width: 760px)').matches) chatHistoryPanel.open = false;
  const lastMessage = messages.lastElementChild;
  if (lastMessage) scrollToMessage(lastMessage);
}

function initializeConversationHistory() {
  const compactLayout = window.matchMedia('(max-width: 760px)');
  chatHistoryPanel.open = !compactLayout.matches;
  compactLayout.addEventListener?.('change', (event) => { chatHistoryPanel.open = !event.matches; });
  renderConversationHistory();
  if (savedConversations.length) openSavedConversation(savedConversations[0].id);
}

function addUserMessage(text) {
  const item = document.createElement('div');
  item.className = 'message user-message';
  item.dataset.question = text;
  const content = document.createElement('div');
  content.className = 'user-message-text';
  content.textContent = text;
  const editButton = document.createElement('button');
  editButton.type = 'button';
  editButton.className = 'edit-message';
  editButton.textContent = 'Edit';
  editButton.setAttribute('aria-label', 'Edit this question');
  editButton.addEventListener('click', () => editUserMessage(item));
  item.append(content, editButton);
  messages.append(item);
  return item;
}

function editUserMessage(message) {
  if (sendButton.disabled) return;
  const priorMessages = Array.from(messages.children);
  const messageIndex = priorMessages.indexOf(message);
  const retainedMessages = activeConversation.messages.slice(0, messageIndex);
  const priorQuestions = retainedMessages
    .filter((entry) => entry.role === 'user' && !entry.reportRequest)
    .map((entry) => entry.text);
  questionHistory.splice(0, questionHistory.length, ...priorQuestions);
  activeConversation.messages = retainedMessages;
  activeConversation.title = retainedMessages.find((entry) => entry.role === 'user')?.text || 'New conversation';
  activeConversation.updatedAt = Date.now();
  while (message.nextElementSibling) {
    releasePdfPreviews(message.nextElementSibling);
    message.nextElementSibling.remove();
  }
  message.remove();
  saveConversationHistory();
  welcome.hidden = messages.children.length > 0;
  input.value = message.dataset.question;
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  input.focus();
}

function addLoadingMessage() {
  const item = document.createElement('div');
  item.className = 'message assistant-message';
  item.innerHTML = '<div class="loading-line" aria-label="Working"><span></span><span></span><span></span></div>';
  messages.append(item);
  return item;
}

function resultChartNumber(value) {
  const text = String(value ?? '').trim();
  if (!text) return null;
  const normalized = text.replace(/[,$£€¥%\s]/g, '');
  const number = Number(normalized);
  return Number.isFinite(number) ? number : null;
}

function chartText(value) {
  return String(value ?? '').replaceAll('_', ' ');
}

function formatChartValue(value) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value);
}

function isListRequest(question) {
  if (/\blist\b/i.test(question)) return true;
  if (/\b(?:count|how many|headcount|number of|average|avg|mean|median|total|sum|minimum|min|max|maximum|highest|lowest|compare|comparison|trend|chart|graph|plot|percentage|percent|ratio|distribution|share)\b/i.test(question)) {
    return false;
  }
  return /\b(?:show|display|give|provide|get)\b.{0,30}\b(?:all|every|everyone)\b/i.test(question)
    || /\b(?:show|display|give|provide|get|find)\b.{0,40}\b(?:employees|staff|people|records|rows)\b/i.test(question)
    || /\b(?:all|every|everyone)\b.{0,30}\b(?:employees?|staff|people|records|rows)\b/i.test(question);
}

function isIdentifierColumn(column) {
  return /\b(?:id|identifier|number)\b/i.test(String(column).replace(/[_-]+/g, ' '));
}

function buildChartSummary(measureName, categoryName, items, chartType) {
  if (items.length < 2) return '';
  const sortedItems = [...items].sort((left, right) => right[1] - left[1]);
  const highest = sortedItems[0];
  const lowest = sortedItems[sortedItems.length - 1];
  const measureLabel = chartText(measureName);
  const categoryLabel = chartText(categoryName);
  if (chartType === 'line') {
    const first = items[0];
    const last = items[items.length - 1];
    const direction = last[1] > first[1] ? 'increased' : last[1] < first[1] ? 'decreased' : 'stayed level';
    return `${measureLabel} ${direction} from ${formatChartValue(first[1])} in ${first[0]} to ${formatChartValue(last[1])} in ${last[0]}. The highest point was ${formatChartValue(highest[1])} in ${highest[0]}.`;
  }
  if (chartType === 'pie') {
    const total = items.reduce((sum, [, value]) => sum + value, 0);
    const share = total ? new Intl.NumberFormat(undefined, { style: 'percent', maximumFractionDigits: 1 }).format(highest[1] / total) : '0%';
    return `${highest[0]} has the largest share of ${measureLabel}, with ${formatChartValue(highest[1])} (${share}) across the ${categoryLabel.toLowerCase()} groups.`;
  }
  return `${highest[0]} has the highest ${measureLabel} at ${formatChartValue(highest[1])}, while ${lowest[0]} has the lowest at ${formatChartValue(lowest[1])}.`;
}

function buildResultChart(columns, rows, question = '') {
  if (rows.length < 2 || isListRequest(question) || columns.some(isIdentifierColumn)) return null;
  const numericIndexes = columns.map((_, index) => index).filter((index) =>
    rows.every((row) => resultChartNumber(row[index]) !== null)
      && !isIdentifierColumn(columns[index])
      && !/(?:^|_)(?:date|month|year|quarter|period|week|day)(?:_|$)/i.test(columns[index])
  );
  if (!numericIndexes.length) return null;

  const categoryIndexes = columns.map((_, index) => index).filter((index) => {
    if (numericIndexes.includes(index)) return false;
    if (isIdentifierColumn(columns[index])) return false;
    const values = rows.map((row) => String(row[index] ?? '').trim()).filter(Boolean);
    const uniqueCount = new Set(values).size;
    return uniqueCount > 1 && uniqueCount <= 100;
  });
  if (!categoryIndexes.length) return null;

  const categoryIndex = categoryIndexes[0];
  const categoryName = columns[categoryIndex];
  const categoryLabels = rows.map((row) => String(row[categoryIndex] ?? '').trim() || 'Unspecified');
  const isTimeSeries = /date|month|year|quarter|period|week|day/i.test(categoryName)
    || categoryLabels.every((label) => /^(?:\d{4}(?:[-/]\d{1,2})?(?:[-/]\d{1,2})?|Q[1-4]\s*\d{4})$/i.test(label));
  const countIndexes = numericIndexes.filter((index) => /count|headcount|employee|employees|records/i.test(columns[index]));
  const averageIndexes = numericIndexes.filter((index) => /average|avg/i.test(columns[index]));
  let measureIndexes = [...new Set([...countIndexes.slice(0, 1), ...averageIndexes.slice(0, 1)])];
  const hasEmployeeCount = countIndexes.some((index) => /employee|headcount/i.test(columns[index]));
  if (hasEmployeeCount) {
    measureIndexes = measureIndexes.filter((index) => !/salary_records|record_count/i.test(columns[index]));
  }
  measureIndexes.push(...numericIndexes.filter((index) => !measureIndexes.includes(index)
    && !(hasEmployeeCount && /salary_records|record_count/i.test(columns[index]))));
  const requestedMeasure = question.match(/\b(?:headcount|count|number of|how many|employees?)\b/i)
    ? /count|headcount|employee|employees|records/i
    : question.match(/\b(?:average|avg)\b/i)
      ? /average|avg/i
      : question.match(/\b(?:salary|salaries|payroll|earning|earnings)\b/i)
        ? /salary|payroll|total|amount/i
        : null;
  if (requestedMeasure) {
    const requestedIndexes = measureIndexes.filter((index) => requestedMeasure.test(columns[index]));
    if (requestedIndexes.length) measureIndexes = requestedIndexes;
  }
  measureIndexes = measureIndexes.slice(0, 1);

  const colors = Array.from({ length: 8 }, (_, index) => `var(--chart-${index + 1})`);
  const charts = document.createDocumentFragment();
  measureIndexes.slice(0, 3).forEach((measureIndex) => {
    const totals = new Map();
    rows.forEach((row) => {
      const label = String(row[categoryIndex] ?? '').trim() || 'Unspecified';
      const value = resultChartNumber(row[measureIndex]);
      if (value !== null) totals.set(label, (totals.get(label) || 0) + value);
    });
    let items = [...totals.entries()];
    if (items.length < 2) return;
    const categoryCount = items.length;
    if (!isTimeSeries) items.sort((left, right) => Math.abs(right[1]) - Math.abs(left[1]));
    items = items.slice(0, 12);

    const measureName = columns[measureIndex];
    const isCount = /count|headcount|employee|employees|records/i.test(measureName);
    const requestsProportions = /\b(?:share|shares|proportion|percentage|percent|distribution)\b/i.test(question);
    const chartType = isTimeSeries
      ? 'line'
      : isCount && requestsProportions && categoryCount <= 6
        ? 'pie'
        : 'bar';
    const figure = document.createElement('figure');
    figure.className = 'result-chart';
    figure.dataset.chartType = chartType;
    figure.setAttribute('aria-label', `${measureName} by ${categoryName} ${chartType} chart`);
    const caption = document.createElement('figcaption');
    caption.className = 'result-chart-title';
    caption.textContent = `${measureName.replaceAll('_', ' ')} by ${categoryName.replaceAll('_', ' ')}`;
    figure.append(caption);

    if (chartType === 'pie') {
      const positiveItems = items.filter(([, value]) => value > 0).sort((left, right) => right[1] - left[1]);
      if (positiveItems.length < 2) return;
      const pieItems = positiveItems.length > 8
        ? [...positiveItems.slice(0, 7), ['Other', positiveItems.slice(7).reduce((sum, [, value]) => sum + value, 0)]]
        : positiveItems;
      const total = pieItems.reduce((sum, [, value]) => sum + value, 0);
      let percentage = 0;
      const segments = pieItems.map(([, value], index) => {
        const start = percentage;
        percentage += value / total * 100;
        return `${colors[index % colors.length]} ${start}% ${percentage}%`;
      });
      const layout = document.createElement('div');
      layout.className = 'result-chart-pie-layout';
      const pie = document.createElement('div');
      pie.className = 'result-chart-pie';
      pie.setAttribute('aria-hidden', 'true');
      pie.style.background = `conic-gradient(${segments.join(', ')})`;
      const legend = document.createElement('ol');
      legend.className = 'result-chart-legend';
      pieItems.forEach(([label, value], index) => {
        const entry = document.createElement('li');
        const swatch = document.createElement('span');
        swatch.className = 'result-chart-swatch';
        swatch.style.background = colors[index % colors.length];
        swatch.setAttribute('aria-hidden', 'true');
        const name = document.createElement('span');
        name.className = 'result-chart-label';
        name.textContent = label;
        const amount = document.createElement('span');
        amount.className = 'result-chart-value';
        amount.textContent = `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value)} (${new Intl.NumberFormat(undefined, { style: 'percent', maximumFractionDigits: 1 }).format(value / total)})`;
        entry.append(swatch, name, amount);
        legend.append(entry);
      });
      layout.append(pie, legend);
      figure.append(layout);
    } else if (chartType === 'line') {
      const values = items.map(([, value]) => value);
      const minimum = Math.min(0, ...values);
      const maximum = Math.max(...values, minimum + 1);
      const width = 640;
      const height = 190;
      const left = 44;
      const right = width - 12;
      const top = 14;
      const bottom = height - 34;
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.classList.add('result-chart-line');
      svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
      svg.setAttribute('role', 'img');
      svg.setAttribute('aria-label', `${measureName} trend by ${categoryName}`);
      for (let step = 0; step < 4; step += 1) {
        const y = top + (bottom - top) * step / 3;
        const grid = document.createElementNS(svg.namespaceURI, 'line');
        grid.setAttribute('x1', String(left));
        grid.setAttribute('x2', String(right));
        grid.setAttribute('y1', String(y));
        grid.setAttribute('y2', String(y));
        grid.classList.add('result-chart-gridline');
        svg.append(grid);
      }
      const points = items.map(([, value], index) => {
        const x = left + (right - left) * index / Math.max(1, items.length - 1);
        const y = bottom - (bottom - top) * (value - minimum) / (maximum - minimum || 1);
        return { x, y, value, label: items[index][0] };
      });
      const line = document.createElementNS(svg.namespaceURI, 'polyline');
      line.setAttribute('points', points.map((point) => `${point.x},${point.y}`).join(' '));
      line.classList.add('result-chart-line-path');
      svg.append(line);
      points.forEach((point, index) => {
        const circle = document.createElementNS(svg.namespaceURI, 'circle');
        circle.setAttribute('cx', String(point.x));
        circle.setAttribute('cy', String(point.y));
        circle.setAttribute('r', '3.5');
        circle.classList.add('result-chart-line-point');
        const pointTitle = document.createElementNS(svg.namespaceURI, 'title');
        pointTitle.textContent = `${point.label}: ${new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(point.value)}`;
        circle.append(pointTitle);
        svg.append(circle);
        if (items.length <= 6 || index % 2 === 0 || index === items.length - 1) {
          const label = document.createElementNS(svg.namespaceURI, 'text');
          label.setAttribute('x', String(point.x));
          label.setAttribute('y', String(height - 10));
          label.setAttribute('text-anchor', 'middle');
          label.classList.add('result-chart-axis-label');
          label.textContent = point.label.length > 11 ? `${point.label.slice(0, 8)}...` : point.label;
          svg.append(label);
        }
      });
      figure.append(svg);
    } else {
      const itemsToShow = items.slice(0, 12);
      const maximum = Math.max(...itemsToShow.map(([, value]) => Math.abs(value)), 0);
      const list = document.createElement('ol');
      list.className = 'result-chart-list';
      itemsToShow.forEach(([label, value]) => {
        const row = document.createElement('li');
        row.className = 'result-chart-row';
        const category = document.createElement('span');
        category.className = 'result-chart-label';
        category.textContent = label;
        const track = document.createElement('span');
        track.className = 'result-chart-track';
        track.setAttribute('aria-hidden', 'true');
        const bar = document.createElement('span');
        bar.className = 'result-chart-bar';
        bar.style.width = `${maximum ? Math.abs(value) / maximum * 100 : 0}%`;
        track.append(bar);
        const amount = document.createElement('span');
        amount.className = 'result-chart-value';
        amount.textContent = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value);
        row.append(category, track, amount);
        list.append(row);
      });
      figure.append(list);
    }
    const chartSummary = document.createElement('p');
    chartSummary.className = 'result-chart-summary';
    chartSummary.textContent = buildChartSummary(measureName, categoryName, items, chartType);
    figure.append(chartSummary);
    charts.append(figure);
  });
  return charts.childElementCount ? charts : null;
}

function resultTableMarkup(result) {
  const numeric = (column) => /salary|count|total|average|amount|employee_id|emp_id|number/i.test(column);
  const thead = result.columns.map((column) => `<th>${escapeHtml(column.replaceAll('_', ' '))}</th>`).join('');
  const tbody = result.rows.map((row) => `<tr>${result.columns.map((column, index) => `<td class="${numeric(column) ? 'numeric' : ''}">${escapeHtml(row[index] ?? '')}</td>`).join('')}</tr>`).join('');
  const truncation = result.truncated ? '<div class="empty-result">Showing the first 2,000 rows.</div>' : '';
  return `<div class="table-scroll"><table class="result-table"><thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table></div>${truncation}`;
}

function renderResults(item, result, question = '') {
  const count = result.rows.length;
  const summary = `<div class="answer-copy">${escapeHtml(result.summary || '')}</div>`;
  if (isListRequest(question) && result.columns.length && count) {
    item.innerHTML = resultTableMarkup(result);
    return;
  }
  if (!result.columns.length || !count) {
    item.innerHTML = summary;
  } else if (count === 1) {
    item.innerHTML = summary;
  } else {
    const chart = buildResultChart(result.columns, result.rows, question);
    if (chart) {
      item.replaceChildren();
      item.append(chart);
    } else {
      item.innerHTML = `${summary}${resultTableMarkup(result)}`;
    }
  }
  const audioButton = document.createElement('button');
  audioButton.type = 'button';
  audioButton.className = 'audio-response';
  audioButton.innerHTML = '<span class="speaker-icon" aria-hidden="true"></span><span class="audio-label">Listen</span>';
  audioButton.disabled = !speechOutputSupported;
  audioButton.title = speechOutputSupported ? 'Read this response aloud' : 'Spoken responses are not supported in this browser';
  audioButton.addEventListener('click', () => speakResponse(result.summary || '', audioButton));
  item.append(audioButton);
}

function renderError(item, message) {
  item.replaceChildren();
  const error = document.createElement('div');
  error.className = 'error-result';
  error.textContent = message;
  item.append(error);
}

function scrollToMessage(message) {
  const conversation = document.querySelector('#conversation');
  const top = message.getBoundingClientRect().top - conversation.getBoundingClientRect().top + conversation.scrollTop;
  conversation.scrollTo({ top, behavior: 'smooth' });
}

async function requestBusinessReport(question) {
  const response = await fetch('/api/report', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question })
  });
  if (!response.ok) {
    const payload = await response.json();
    throw new Error(payload.error || 'The business report could not be generated.');
  }
  const pdf = await response.blob();
  const disposition = response.headers.get('Content-Disposition') || '';
  const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || 'business_report.pdf';
  return { pdf, filename };
}

async function pdfToBase64(pdf) {
  const bytes = new Uint8Array(await pdf.arrayBuffer());
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function base64ToPdf(value) {
  const binary = atob(value);
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return new Blob([bytes], { type: 'application/pdf' });
}

function releasePdfPreviews(container) {
  container.querySelectorAll('.pdf-preview').forEach((preview) => {
    if (preview.dataset.objectUrl) URL.revokeObjectURL(preview.dataset.objectUrl);
    preview.resizeObserver?.disconnect();
    preview.renderTask?.cancel();
    preview.pdfLoadingTask?.destroy();
  });
}

let pdfJsImport;

function loadPdfJs() {
  if (!pdfJsImport) {
    pdfJsImport = import('/vendor/pdfjs/pdf.min.mjs').then((pdfjs) => {
      pdfjs.GlobalWorkerOptions.workerSrc = '/vendor/pdfjs/pdf.worker.min.mjs';
      return pdfjs;
    }).catch((error) => {
      pdfJsImport = null;
      throw error;
    });
  }
  return pdfJsImport;
}

function renderReportPreview(item, pdf, filename, question) {
  const objectUrl = URL.createObjectURL(pdf);
  const preview = document.createElement('section');
  preview.className = 'pdf-preview';
  preview.dataset.objectUrl = objectUrl;

  const documentHeader = document.createElement('div');
  documentHeader.className = 'pdf-preview-header';
  const fileIcon = document.createElement('span');
  fileIcon.className = 'pdf-file-icon';
  fileIcon.textContent = 'PDF';
  const documentDetails = document.createElement('div');
  documentDetails.className = 'pdf-document-details';
  const documentName = document.createElement('strong');
  documentName.className = 'pdf-document-name';
  documentName.textContent = filename;
  const documentMeta = document.createElement('span');
  documentMeta.className = 'pdf-document-meta';
  documentMeta.textContent = 'Loading document preview...';
  documentDetails.append(documentName, documentMeta);
  documentHeader.append(fileIcon, documentDetails);

  const actions = document.createElement('div');
  actions.className = 'pdf-preview-actions';

  const downloadButton = document.createElement('button');
  downloadButton.type = 'button';
  downloadButton.className = 'pdf-action';
  downloadButton.textContent = 'Download';
  const downloadPdf = () => {
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
  };
  downloadButton.addEventListener('click', downloadPdf);

  const printButton = document.createElement('button');
  printButton.type = 'button';
  printButton.className = 'pdf-action';
  printButton.textContent = 'Print';

  const emailButton = document.createElement('button');
  emailButton.type = 'button';
  emailButton.className = 'pdf-action';
  emailButton.textContent = 'Email PDF';

  const emailForm = document.createElement('form');
  emailForm.className = 'pdf-email-form';
  emailForm.hidden = true;
  const emailLabel = document.createElement('label');
  emailLabel.append(document.createTextNode('Send report to'));
  const emailInput = document.createElement('input');
  emailInput.type = 'email';
  emailInput.name = 'recipient';
  emailInput.autocomplete = 'email';
  emailInput.maxLength = 254;
  emailInput.required = true;
  emailInput.placeholder = 'name@example.com';
  emailLabel.append(emailInput);
  const sendEmailButton = document.createElement('button');
  sendEmailButton.type = 'submit';
  sendEmailButton.className = 'pdf-action';
  sendEmailButton.textContent = 'Send email';
  emailForm.append(emailLabel, sendEmailButton);

  const status = document.createElement('div');
  status.className = 'pdf-preview-status';
  status.setAttribute('role', 'status');
  emailButton.addEventListener('click', () => {
    emailForm.hidden = !emailForm.hidden;
    if (!emailForm.hidden) emailInput.focus();
  });
  emailForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const recipient = emailInput.value.trim();
    sendEmailButton.disabled = true;
    status.textContent = 'Sending PDF...';
    status.dataset.state = 'pending';
    try {
      const response = await fetch('/api/report/email', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, recipient })
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'The PDF could not be emailed.');
      status.textContent = `PDF sent to ${recipient}.`;
      status.dataset.state = 'success';
      emailInput.value = '';
    } catch (error) {
      status.textContent = error.message || 'The PDF could not be emailed.';
      status.dataset.state = 'error';
    } finally {
      sendEmailButton.disabled = false;
    }
  });

  const viewerToolbar = document.createElement('div');
  viewerToolbar.className = 'pdf-viewer-toolbar';
  const pageControls = document.createElement('div');
  pageControls.className = 'pdf-viewer-controls';
  const previousPageButton = document.createElement('button');
  previousPageButton.type = 'button';
  previousPageButton.className = 'pdf-viewer-control';
  previousPageButton.textContent = '‹';
  previousPageButton.title = 'Previous page';
  previousPageButton.setAttribute('aria-label', 'Previous page');
  previousPageButton.disabled = true;
  const pageIndicator = document.createElement('span');
  pageIndicator.className = 'pdf-page-indicator';
  pageIndicator.textContent = 'Page 1 of 1';
  const nextPageButton = document.createElement('button');
  nextPageButton.type = 'button';
  nextPageButton.className = 'pdf-viewer-control';
  nextPageButton.textContent = '›';
  nextPageButton.title = 'Next page';
  nextPageButton.setAttribute('aria-label', 'Next page');
  nextPageButton.disabled = true;
  pageControls.append(previousPageButton, pageIndicator, nextPageButton);

  const zoomControls = document.createElement('div');
  zoomControls.className = 'pdf-viewer-controls';
  const zoomOutButton = document.createElement('button');
  zoomOutButton.type = 'button';
  zoomOutButton.className = 'pdf-viewer-control';
  zoomOutButton.textContent = '−';
  zoomOutButton.title = 'Zoom out';
  zoomOutButton.setAttribute('aria-label', 'Zoom out');
  const zoomIndicator = document.createElement('span');
  zoomIndicator.className = 'pdf-zoom-indicator';
  zoomIndicator.textContent = '100%';
  const zoomInButton = document.createElement('button');
  zoomInButton.type = 'button';
  zoomInButton.className = 'pdf-viewer-control';
  zoomInButton.textContent = '+';
  zoomInButton.title = 'Zoom in';
  zoomInButton.setAttribute('aria-label', 'Zoom in');
  zoomControls.append(zoomOutButton, zoomIndicator, zoomInButton);
  viewerToolbar.append(pageControls, zoomControls);

  const pageSurface = document.createElement('div');
  pageSurface.className = 'pdf-page-surface';
  const canvas = document.createElement('canvas');
  canvas.className = 'pdf-page-canvas';
  canvas.setAttribute('aria-label', 'PDF page preview');
  pageSurface.append(canvas);

  let pdfDocument = null;
  let currentPage = 1;
  let zoom = 1;
  let renderVersion = 0;
  let resizeFrame = 0;

  const updateControls = () => {
    pageIndicator.textContent = `Page ${currentPage} of ${pdfDocument?.numPages || 1}`;
    previousPageButton.disabled = currentPage <= 1;
    nextPageButton.disabled = !pdfDocument || currentPage >= pdfDocument.numPages;
    zoomIndicator.textContent = `${Math.round(zoom * 100)}%`;
    zoomOutButton.disabled = zoom <= 0.6;
    zoomInButton.disabled = zoom >= 2;
  };

  const renderPage = async () => {
    if (!pdfDocument) return;
    const version = ++renderVersion;
    preview.renderTask?.cancel();
    try {
      const page = await pdfDocument.getPage(currentPage);
      if (version !== renderVersion) return;
      const baseViewport = page.getViewport({ scale: 1 });
      const fitScale = Math.min(1.2, Math.max(0.25, (pageSurface.clientWidth - 32) / baseViewport.width));
      const viewport = page.getViewport({ scale: fitScale * zoom });
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.ceil(viewport.width * pixelRatio);
      canvas.height = Math.ceil(viewport.height * pixelRatio);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      const context = canvas.getContext('2d', { alpha: false });
      preview.renderTask = page.render({
        canvasContext: context,
        viewport,
        transform: pixelRatio === 1 ? null : [pixelRatio, 0, 0, pixelRatio, 0, 0],
        background: '#ffffff'
      });
      await preview.renderTask.promise;
      if (version === renderVersion) status.textContent = '';
    } catch (error) {
      if (error.name !== 'RenderingCancelledException') {
        status.textContent = 'Preview could not be rendered. Download the PDF to open it.';
      }
    }
  };

  previousPageButton.addEventListener('click', () => {
    currentPage = Math.max(1, currentPage - 1);
    updateControls();
    void renderPage();
  });
  nextPageButton.addEventListener('click', () => {
    currentPage = Math.min(pdfDocument?.numPages || 1, currentPage + 1);
    updateControls();
    void renderPage();
  });
  zoomOutButton.addEventListener('click', () => {
    zoom = Math.max(0.6, Math.round((zoom - 0.2) * 10) / 10);
    updateControls();
    void renderPage();
  });
  zoomInButton.addEventListener('click', () => {
    zoom = Math.min(2, Math.round((zoom + 0.2) * 10) / 10);
    updateControls();
    void renderPage();
  });
  printButton.addEventListener('click', async () => {
    if (!pdfDocument) return;
    const printWindow = window.open('', '_blank');
    if (!printWindow) {
      status.textContent = 'Allow pop-ups to print this report.';
      return;
    }
    printButton.disabled = true;
    status.textContent = 'Preparing pages for printing...';
    try {
      const printDocument = printWindow.document;
      printDocument.title = filename;
      const printStyles = printDocument.createElement('style');
      printStyles.textContent = '@page{margin:0}body{margin:0;background:#fff}.print-page{page-break-after:always;break-after:page}.print-page:last-child{page-break-after:auto;break-after:auto}.print-page img{display:block;width:100%;height:auto}';
      printDocument.head.append(printStyles);
      const printBody = printDocument.body;
      printBody.replaceChildren();
      for (let pageNumber = 1; pageNumber <= pdfDocument.numPages; pageNumber += 1) {
        const printPage = await pdfDocument.getPage(pageNumber);
        const printViewport = printPage.getViewport({ scale: 1.5 });
        const printCanvas = printDocument.createElement('canvas');
        printCanvas.width = Math.ceil(printViewport.width);
        printCanvas.height = Math.ceil(printViewport.height);
        await printPage.render({ canvasContext: printCanvas.getContext('2d'), viewport: printViewport }).promise;
        const pageWrapper = printDocument.createElement('section');
        pageWrapper.className = 'print-page';
        const image = printDocument.createElement('img');
        image.src = printCanvas.toDataURL('image/png');
        image.alt = `Page ${pageNumber}`;
        pageWrapper.append(image);
        printBody.append(pageWrapper);
      }
      status.textContent = '';
      printWindow.focus();
      printWindow.setTimeout(() => printWindow.print(), 250);
    } catch {
      printWindow.close();
      status.textContent = 'Could not prepare this PDF for printing.';
    } finally {
      printButton.disabled = false;
    }
  });

  actions.append(downloadButton, printButton, emailButton);
  preview.append(documentHeader, actions, emailForm, viewerToolbar, status, pageSurface);
  item.append(preview);

  preview.resizeObserver = new ResizeObserver(() => {
    window.cancelAnimationFrame(resizeFrame);
    resizeFrame = window.requestAnimationFrame(() => { void renderPage(); });
  });
  preview.resizeObserver.observe(pageSurface);
  status.textContent = 'Loading PDF preview...';
  void loadPdfJs().then(async (pdfjs) => {
    const loadingTask = pdfjs.getDocument({ data: await pdf.arrayBuffer() });
    preview.pdfLoadingTask = loadingTask;
    pdfDocument = await loadingTask.promise;
    documentMeta.textContent = `${pdfDocument.numPages} page${pdfDocument.numPages === 1 ? '' : 's'} · PDF document`;
    updateControls();
    await renderPage();
  }).catch(() => {
    documentMeta.textContent = 'PDF preview unavailable';
    status.textContent = 'Could not load the PDF preview. Download the file to open it.';
  });
}

function isReportRequest(question) {
  const normalizedQuestion = question.trim()
    .replace(/\bgenrate\b/gi, 'generate')
    .replace(/\bprocessional\b|\bproffesional\b|\bprofesional\b/gi, 'professional');
  if (/\b(?:don't|do not|never|no)\b[\s\S]*\b(?:pdf|report)\b/i.test(normalizedQuestion)) return false;
  return /^(?:please\s+)?(?:(?:can|could|would)\s+(?:you|i)\s+)?(?:please\s+)?(?:generate|create|make|build|download|export|prepare|provide|show|view|get|give|see)\b[\s\S]*\b(?:pdf|report)\b/i.test(normalizedQuestion)
    || /^(?:can|could|may)\s+i\s+(?:have|get|see)\b[\s\S]*\b(?:pdf|report)\b/i.test(normalizedQuestion)
    || /\b(?:i|we)\s+(?:want|need|would like)\b[\s\S]*\b(?:pdf|report)\b/i.test(normalizedQuestion)
    || /\b(?:pdf|report)\s+(?:please|for me|now)\b/i.test(normalizedQuestion)
    || /^(?:pdf(?:\s+report)?|business report|report(?:\s+pdf)?)(?:\s+please)?[.!?]*$/i.test(normalizedQuestion);
}

async function submitQuestion(question) {
  const text = question.trim();
  if (!text || sendButton.disabled) return;
  const reportRequest = isReportRequest(text);
  const history = questionHistory.slice(-6);
  if (!reportRequest) questionHistory.push(text);
  welcome.hidden = true;
  const userMessage = addUserMessage(text);
  if (reportRequest) userMessage.dataset.reportRequest = 'true';
  appendConversationMessage({ role: 'user', text, reportRequest });
  input.value = '';
  input.style.height = 'auto';
  sendButton.disabled = true;
  renderConversationHistory();
  messages.querySelectorAll('.edit-message').forEach((button) => { button.disabled = true; });
  const responseNode = addLoadingMessage();
  scrollToMessage(userMessage);
  try {
    if (reportRequest) {
      const { pdf, filename } = await requestBusinessReport(text);
      const encodedPdf = await pdfToBase64(pdf);
      responseNode.replaceChildren();
      responseNode.classList.add('result-message');
      const reportText = 'Your PDF report is ready to preview.';
      const answer = document.createElement('div');
      answer.className = 'answer-copy';
      answer.textContent = reportText;
      responseNode.append(answer);
      renderReportPreview(responseNode, pdf, filename, text);
      appendConversationMessage({ role: 'assistant', kind: 'report', text: reportText, question: text, filename, pdf: encodedPdf });
    } else {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: text, history })
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'The query could not be completed.');
      responseNode.classList.add('result-message');
      renderResults(responseNode, payload, text);
      appendConversationMessage({ role: 'assistant', kind: 'result', result: payload, question: text });
    }
  } catch (error) {
    const errorText = error.message || 'Could not reach the local query service.';
    renderError(responseNode, errorText);
    appendConversationMessage({ role: 'assistant', kind: 'error', text: errorText });
  } finally {
    sendButton.disabled = false;
    messages.querySelectorAll('.edit-message').forEach((button) => { button.disabled = false; });
    renderConversationHistory();
    input.focus();
    scrollToMessage(userMessage);
  }
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  submitQuestion(input.value);
});

input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

input.addEventListener('input', () => {
  updateInputHeight();
});

document.querySelectorAll('.suggestion').forEach((button) => {
  button.addEventListener('click', () => submitQuestion(button.dataset.question));
});

document.querySelector('#new-chat').addEventListener('click', () => {
  if (sendButton.disabled) return;
  activeConversation = createConversation();
  releasePdfPreviews(messages);
  messages.replaceChildren();
  questionHistory.length = 0;
  welcome.hidden = false;
  input.value = '';
  renderConversationHistory();
  if (window.matchMedia('(max-width: 760px)').matches) chatHistoryPanel.open = false;
  input.focus();
});

initializeConversationHistory();

async function checkConnection() {
  try {
    const response = await fetch('/api/health', { cache: 'no-store' });
    const health = await response.json();
    updateTranscriptionAvailability(response.ok && health.transcription_ready);
  } catch {
    updateTranscriptionAvailability(false);
  }
}

checkConnection();
setInterval(checkConnection, 30_000);
